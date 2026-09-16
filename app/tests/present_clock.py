"""Bounded real X11 Present-MSC timing probe; returns numeric timing only.

The packed CompleteNotify layout is the public libxcb-present ABI, including
libxcb's inserted full_sequence at offset32 and unaligned MSC at offset36.
No window images, page contents or credentials are read.
"""
import ctypes as C
import select
import statistics
import time


class Cookie(C.Structure):
    _fields_ = [('sequence', C.c_uint)]


class Iterator(C.Structure):
    _fields_ = [('data', C.c_void_p), ('rem', C.c_int), ('index', C.c_int)]


class Complete(C.Structure):
    _pack_ = 1
    _fields_ = [('response', C.c_uint8), ('extension', C.c_uint8),
                ('sequence', C.c_uint16), ('length', C.c_uint32),
                ('event_type', C.c_uint16), ('kind', C.c_uint8), ('mode', C.c_uint8),
                ('event', C.c_uint32), ('window', C.c_uint32), ('serial', C.c_uint32),
                ('ust', C.c_uint64), ('full_sequence', C.c_uint32), ('msc', C.c_uint64)]


def measure(count=12):
    assert 2 <= count <= 120
    assert Complete.msc.offset == 36 and C.sizeof(Complete) == 44
    xcb, present, libc = C.CDLL('libxcb.so.1'), C.CDLL('libxcb-present.so.0'), C.CDLL(None)
    def bind(library, name, result, arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
        return function
    pointer, u32, u64 = C.c_void_p, C.c_uint32, C.c_uint64
    connect = bind(xcb, 'xcb_connect', pointer, [C.c_char_p, C.POINTER(C.c_int)])
    disconnect = bind(xcb, 'xcb_disconnect', None, [pointer])
    has_error = bind(xcb, 'xcb_connection_has_error', C.c_int, [pointer])
    setup = bind(xcb, 'xcb_get_setup', pointer, [pointer])
    roots = bind(xcb, 'xcb_setup_roots_iterator', Iterator, [pointer])
    generate = bind(xcb, 'xcb_generate_id', u32, [pointer])
    flush = bind(xcb, 'xcb_flush', C.c_int, [pointer])
    poll = bind(xcb, 'xcb_poll_for_event', pointer, [pointer])
    fd = bind(xcb, 'xcb_get_file_descriptor', C.c_int, [pointer])
    free = bind(libc, 'free', None, [pointer])
    version = bind(present, 'xcb_present_query_version', Cookie, [pointer, u32, u32])
    version_reply = bind(present, 'xcb_present_query_version_reply', pointer, [pointer, Cookie, pointer])
    select_input = bind(present, 'xcb_present_select_input', Cookie, [pointer, u32, u32, u32])
    notify = bind(present, 'xcb_present_notify_msc', Cookie, [pointer, u32, u32, u64, u64, u64])
    connection = connect(None, None)
    try:
        assert connection and not has_error(connection), 'Private X11 connection failed'
        reply = version_reply(connection, version(connection, 1, 0), None)
        assert reply, 'Present extension unavailable'
        free(reply)
        iterator = roots(setup(connection))
        assert iterator.rem > 0 and iterator.data
        root = C.cast(iterator.data, C.POINTER(u32)).contents.value
        event_id = generate(connection)
        select_input(connection, event_id, root, 2)
        msc, arrivals = 0, []
        for serial in range(count+1):
            notify(connection, root, serial, 0 if serial == 0 else msc+1, 0, 0)
            flush(connection)
            deadline = time.monotonic()+2.5
            while True:
                assert time.monotonic() < deadline, 'Present completion timed out'
                event = poll(connection)
                if event:
                    try:
                        header = C.string_at(event, 12)
                        assert header[0] != 0, 'X11 rejected Present clock request'
                        if header[0] & 127 == 35:
                            value = C.cast(event, C.POINTER(Complete)).contents
                            if value.event_type == 1 and value.kind == 1 and value.event == event_id and value.serial == serial:
                                msc = value.msc
                                arrivals.append(time.monotonic())
                                break
                    finally:
                        free(event)
                    continue
                remaining = deadline-time.monotonic()
                assert remaining > 0, 'Present completion timed out'
                select.select([fd(connection)], [], [], remaining)
        gaps = [b-a for a,b in zip(arrivals, arrivals[1:])]
        return {'samples':len(gaps), 'hz':round(len(gaps)/(arrivals[-1]-arrivals[0]),2),
                'median_gap_ms':round(statistics.median(gaps)*1000,2),
                'max_gap_ms':round(max(gaps)*1000,2)}
    finally:
        if connection:
            disconnect(connection)
