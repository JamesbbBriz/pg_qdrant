"""Bounded read-only task iterator for initial-namespace kernel PID identity.

Requires x86_64 Linux BTF, CAP_BPF and CAP_PERFMON. No pinned links, maps,
tracepoint hooks, signals or kernel writes; all descriptors close before return."""
import ctypes
import struct
import os
from pathlib import Path
import hashlib

if not __debug__:
    raise RuntimeError('optimized Python is unsupported for kernel identity observations')

def read_kernel_identity(request):
    assert os.uname().machine == 'x86_64'
    assert 0 < request['inner_pid'] < 2 ** 31 and 0 < request['namespace_inode'] < 2 ** 32 and (0 < request['start_ticks'] < 2 ** 31) and (request['clock_ticks'] in (100, 250, 1000))
    with Path('/sys/kernel/btf/vmlinux').open('rb') as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    assert len(raw) <= 32 * 1024 * 1024
    magic, version, flags, header, to, tl, so, sl = struct.unpack_from('<HBBIIIII', raw)
    assert magic == 0xeb9f and version == 1 and flags == 0 and header == 24
    assert header + to + tl <= len(raw) and header + so + sl <= len(raw)
    strings = raw[header + so:header + so + sl]
    off = header + to
    end = off + tl
    tid = 1
    types = {}
    func = None
    aggregates = {}
    aggregate_sizes = {}
    named = {}

    def name_at(n):
        end = strings.find(b'\x00', n)
        assert 0 <= n <= end < len(strings)
        return strings[n:end].decode()
    while off < end:
        n, info, size = struct.unpack_from('<III', raw, off)
        off += 12
        kind = info >> 24 & 31
        v = info & 65535
        name = name_at(n)
        if name == 'bpf_iter_task' and kind == 12:
            func = tid
        if kind in (4, 5):
            members = []
            for i in range(v):
                nm, typ, bit = struct.unpack_from('<III', raw, off + i * 12)
                members.append((name_at(nm), typ, bit & 16777215))
            aggregates[tid] = members
            aggregate_sizes[tid] = size
            if name:
                named[name] = tid
        off += {1: 4, 2: 0, 3: 12, 4: v * 12, 5: v * 12, 6: v * 8, 7: 0, 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: v * 8, 14: 4, 15: v * 12, 16: 0, 17: 4, 18: 0, 19: v * 12}[kind]
        tid += 1
    assert off == end and func

    def flatten(t, base=0, depth=0):
        assert depth < 8
        fields = {}
        for name, typ, bit in aggregates[t]:
            if name:
                if bit % 8 == 0:
                    fields[name] = base + bit // 8
            elif typ in aggregates:
                assert bit % 8 == 0
                fields.update(flatten(typ, base + bit // 8, depth + 1))
        return fields
    for name in ('task_struct', 'pid', 'upid', 'pid_namespace', 'ns_common', 'bpf_iter__task', 'bpf_iter_meta'):
        types[name] = flatten(named[name])
    assert aggregate_sizes[named['upid']] == 16
    assert types['upid']['nr'] == 0 and types['upid']['ns'] == 8
    code = []
    labels = {}
    patch = []

    def emit(op, d=0, s=0, off=0, imm=0):
        code.append([op, d | s << 4, off, imm])

    def jump(op, d, s=0, imm=0, label='exit'):
        patch.append((len(code), label))
        emit(op, d, s, 0, imm)

    def read(dst, size, reg, offset):
        # BPF_MOV64_REG, ADD64_IMM, MOV64_IMM, then probe_read_kernel (113).
        emit(191, 1, 10)
        emit(7, 1, imm=dst)
        emit(183, 2, imm=size)
        emit(191, 3, reg)
        if offset:
            emit(7, 3, imm=offset)
        emit(133, imm=113)
        jump(85, 0, imm=0)
    # r6=context, r7=task. All scratch and returned bytes are on a 64-byte stack.
    # Only the exact innermost PID, namespace inode and start tick may emit data.
    emit(191, 6, 1)
    emit(121, 7, 6, types['bpf_iter__task']['task'])
    jump(21, 7, imm=0)
    for pos in (-8, -16, -24, -32, -40, -48, -56, -64):
        emit(122, 10, off=pos, imm=0)
    read(-40, 8, 7, types['task_struct']['thread_pid'])
    emit(121, 8, 10, -40)
    jump(21, 8, imm=0)
    read(-48, 4, 8, types['pid']['level'])
    # At most 32 nested namespaces; each kernel struct upid is 16 bytes.
    emit(97, 9, 10, -48)
    jump(37, 9, imm=32)
    emit(103, 9, imm=4)
    emit(15, 8, 9)
    read(-64, 16, 8, types['pid']['numbers'])
    emit(97, 9, 10, -64)
    jump(85, 9, imm=request['inner_pid'])
    emit(123, 10, 9, -24)
    emit(121, 8, 10, -56)
    jump(21, 8, imm=0)
    read(-32, 4, 8, types['pid_namespace']['ns'] + types['ns_common']['inum'])
    emit(97, 9, 10, -32)
    emit(180, 8, imm=ctypes.c_int32(request['namespace_inode']).value)
    jump(93, 9, 8)
    read(-8, 8, 7, types['task_struct']['start_boottime'])
    emit(121, 9, 10, -8)
    emit(55, 9, imm=1000000000 // request['clock_ticks'])
    jump(85, 9, imm=request['start_ticks'])
    # task_struct.pid/tgid are initial-namespace IDs; only a leader is accepted.
    read(-16, 4, 7, types['task_struct']['pid'])
    read(-12, 4, 7, types['task_struct']['tgid'])
    emit(97, 8, 10, -16)
    emit(97, 9, 10, -12)
    jump(93, 8, 9)
    emit(121, 1, 6, types['bpf_iter__task']['meta'])
    emit(121, 1, 1, types['bpf_iter_meta']['seq'])
    emit(191, 2, 10)
    emit(7, 2, imm=-32)
    emit(183, 3, imm=32)
    emit(133, imm=127)  # seq_write of one initialized 32-byte identity record.
    labels['exit'] = len(code)
    emit(183, 0, imm=0)
    emit(149)
    for i, label in patch:
        code[i][2] = labels[label] - i - 1
    instructions = b''.join((struct.pack('<BBhi', *x) for x in code))
    insns = ctypes.create_string_buffer(instructions)
    lic = ctypes.create_string_buffer(b'GPL')
    log = ctypes.create_string_buffer(65536)
    attr = ctypes.create_string_buffer(144)
    # x86_64 UAPI: BPF_PROG_TYPE_TRACING=26, BPF_TRACE_ITER=28.
    struct.pack_into('<IIQQIIQ', attr, 0, 26, len(code), ctypes.addressof(insns), ctypes.addressof(lic), 1, 65536, ctypes.addressof(log))
    struct.pack_into('<I', attr, 68, 28)
    struct.pack_into('<I', attr, 108, func)
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    fds = []

    def bpf(cmd, attr):
        fd = lib.syscall(ctypes.c_long(321), ctypes.c_int(cmd), ctypes.byref(attr), ctypes.c_uint(len(attr)))
        assert fd >= 0, (cmd, ctypes.get_errno(), log.value.decode()[-6000:])
        fds.append(fd)
        return fd
    try:
        prog = bpf(5, attr)
        linkattr = ctypes.create_string_buffer(32)
        struct.pack_into('<IIII', linkattr, 0, prog, 0, 28, 0)
        link = bpf(28, linkattr)
        itattr = ctypes.create_string_buffer(8)
        struct.pack_into('<II', itattr, 0, link, 0)
        it = bpf(33, itattr)
        result = b''
        while True:
            piece = os.read(it, 4096)
            if not piece:
                break
            result += piece
            assert len(result) <= 4096
        assert len(result) == 32, (len(result), request)
        ns, inner, pid, tgid, boot = struct.unpack('<QQIIQ', result)
        assert ns == request['namespace_inode'] and inner == request['inner_pid'] and (pid == tgid) and (pid > 0) and (boot // (1000000000 // request['clock_ticks']) == request['start_ticks'])
        return dict(namespace_inode=ns, inner_pid=inner, kernel_pid=pid, kernel_tgid=tgid, start_boottime_ns=boot, btf_function=func, instructions_sha256=hashlib.sha256(instructions).hexdigest(), btf_sha256=hashlib.sha256(raw).hexdigest())
    finally:
        for fd in reversed(fds):
            os.close(fd)
