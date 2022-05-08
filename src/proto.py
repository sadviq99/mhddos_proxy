import asyncio
from enum import Enum
import errno
import time
from types import GeneratorType
from typing import Any, Callable, Generator, Optional, Tuple

from .core import logger, CONN_PROBE_PERIOD
from .targets import TargetStats


FloodSpecGen = Generator[Tuple[int, Any], None, None]


class FloodOp:
    WRITE = 0
    READ  = 1
    SLEEP = 2


class FloodSpecType(Enum):
    GENERATOR = 0
    BYTES = 1
    CALLABLE = 2


class FloodSpec:

    @classmethod
    def from_any(cls, spec_type: FloodSpecType, spec, *args) -> FloodSpecGen:
        if spec_type == FloodSpecType.GENERATOR:
            return spec
        if spec_type == FloodSpecType.BYTES:
            return cls.from_bytes(spec, *args)
        if spec_type == FloodSpecType.CALLABLE:
            return cls.from_callable(spec, *args)
        raise ValueError(f"Don't know how to create spec from {type(spec)}")

    @staticmethod
    def from_bytes(packet: bytes, num_packets: int) -> FloodSpecGen:
        packet_size = len(packet)
        for _ in range(num_packets):
            yield FloodOp.WRITE, (packet, packet_size)

    @staticmethod
    def from_callable(packet: Callable[[], bytes], num_packets: int) -> FloodSpecGen:
        for _ in range(num_packets):
            _packet: bytes = packet()
            yield FloodOp.WRITE, (_packet, len(_packet))


# XXX: add instrumentation to keep track of connection lifetime,
#      number of ops per open connection, and more
class FloodIO(asyncio.Protocol):

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_close: asyncio.Future,
        stats: TargetStats,
        settings: "AttackSettings",
        flood_spec: FloodSpecGen,
        on_connect: Optional[asyncio.Future] = None,
        debug: bool = False,
    ):
        self._loop = loop
        self._stats = stats
        self._flood_spec = flood_spec
        self._settings = settings
        self._on_close: asyncio.Future = on_close
        self._on_close.add_done_callback(self._handle_cancellation)
        self._debug = debug
        self._on_connect = on_connect
        self._transport = None
        self._handle = None
        self._paused: bool = False
        self._paused_at: Optional[int] = None
        self._read_waiting: bool = False
        self._return_code: bool = False
        self._connected_at: Optional[int] = None
        self._probe_handle = None
        self._num_steps: int = 0

    def connection_made(self, transport) -> None:
        self._stats.track_open_connection()
        self._connected_at = time.perf_counter()
        if self._on_connect and not self._on_connect.done():
            self._on_connect.set_result(True)
        self._transport = transport
        self._transport.set_write_buffer_limits(high=self._settings.high_watermark)
        if hasattr(self._transport, "pause_reading"):
            self._transport.pause_reading()
        self._handle = self._loop.call_soon(self._step)
        self._prob_handle = self._loop.call_later(CONN_PROBE_PERIOD, self._probe)

    def _probe(self) -> None:
        # the approach with "probing" instead of direct timeouts tracking (e.g.
        # with loop.call_later) is used to decrease pressure on the event loop.
        # most drains take < 0.1 seconds, which means that each connection is
        # going to generate too many timers/callbacks during normal operations.
        # probing each 5 seconds allows to catch timeouts with ~5s precision while
        # keeping number of callbacks relatively low
        self._probe_handle = None
        if not self._transport:
            return
        if self._paused_at is not None:
            resumed_after = time.time() - self._paused_at
            if resumed_after > self._settings.drain_timeout_seconds:
                # XXX: it might be the case that network is overwhelmed, which means
                #      it's gonna be wise to track special status for the scheduler
                #      to delay re-launch of the task
                self._transport.abort()
                self._transport = None
                if self._debug:
                    target, method, _ = self._stats.target
                    logger.info(
                        f"Writing resumed too late (bailing)\t{target.human_repr()}\t{method}"
                        f"\t{resumed_after}\t{self._num_steps}")
                return
        self._probe_handle = self._loop.call_later(5, self._probe)

    def data_received(self, data) -> None:
        # overall, we don't use data at all
        # do something smarter when corresponding opcode is introduced
        # we also don't track size of the data received. the only use
        # for the read opcode right now is to make sure something was
        # read from the network. in such a case, use of operations like
        # read(1) does not make much of sense (as the data is already
        # buffered anyways)
        if not self._transport:
            return
        if hasattr(self._transport, "pause_reading"):
            self._transport.pause_reading()
        if self._read_waiting:
            self._read_waiting = False
            self._loop.call_soon(self._step)

    def eof_received(self) -> None:
        pass

    def connection_lost(self, exc) -> None:
        self._stats.track_close_connection()
        self._transport = None
        if self._handle:
            self._handle.cancel()
        if self._probe_handle:
            self._probe_handle.cancel()
        if self._on_close.done():
            return
        if exc is None:
            self._on_close.set_result(self._return_code)
        elif isinstance(exc, IOError) and exc.errno == errno.EPIPE:
            # EPIPE exception here means that the connection was interrupted
            # we still consider connection to the target "succesful", no need
            # to bump our failure budget
            # As we typically pause reading, it's unlikely to process EOF from
            # the peer properly. Thus EPIPE instead is expected to happen.
            self._on_close.set_result(self._return_code)
        else:
            self._on_close.set_exception(exc)

    def pause_writing(self) -> None:
        if self._paused:
            return
        self._paused, self._paused_at = True, time.time()

    def resume_writing(self) -> None:
        if not self._paused:
            return
        self._paused, self._paused_at = False, None
        if not self._transport:
            return
        if self._handle is None:
            # XXX: there's an interesting race condition here
            #      as it might happen multiple times
            self._handle = self._loop.call_soon(self._step)

    def _step(self, resumed: bool = False) -> None:
        if not self._transport:
            return
        self._num_steps += 1
        self._return_code = True
        try:
            # XXX: this is actually less flexible than would be necessary
            #      as we still need to keep track of current op & stash
            op, args = next(self._flood_spec)
            if op == FloodOp.WRITE:
                packet, size = args
                self._transport.write(packet)
                self._stats.track(1, size)
                self._handle = None
                if not self._paused:
                    self._handle = self._loop.call_soon(self._step)
            elif op == FloodOp.SLEEP:
                self._handle = self._loop.call_later(args, self._step)
            elif op == FloodOp.READ:
                # XXX: what about read timeout, do we even need it?
                #      (it might be okay as long as connection is consumed)
                self._read_waiting = True
                if hasattr(self._transport, "resume_reading"):
                    self._transport.resume_reading()
            else:
                raise ValueError(f"Unknown flood opcode {op}")
        except StopIteration:
            self._transport.close()
            self._transport = None

    def _handle_cancellation(self, on_close):
        if on_close.cancelled() and self._transport and not self._transport.is_closing():
            self._transport.abort()
            self._transport = None

