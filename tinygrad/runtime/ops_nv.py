from __future__ import annotations
import os, ctypes, contextlib, re, functools, mmap, struct, array, sys, weakref
assert sys.platform != 'win32'
from typing import cast, Union, ClassVar
from dataclasses import dataclass
from tinygrad.runtime.support.hcq import HCQCompiled, HCQAllocator, HCQBuffer, HWQueue, CLikeArgsState, HCQProgram, HCQSignal, BumpAllocator
from tinygrad.runtime.support.hcq import MMIOInterface, FileIOInterface, MOCKGPU
from tinygrad.uop.ops import sint
from tinygrad.device import BufferSpec, CPUProgram
from tinygrad.helpers import getenv, mv_address, round_up, data64, data64_le, DEBUG, prod, OSX, to_mv, hi32, lo32
from tinygrad.renderer.ptx import PTXRenderer
from tinygrad.renderer.cstyle import NVRenderer
from tinygrad.runtime.support.compiler_cuda import CUDACompiler, PTXCompiler, PTX, NVPTXCompiler, NVCompiler
from tinygrad.runtime.autogen import nv_gpu
from tinygrad.runtime.support.elf import elf_loader
if getenv("IOCTL"): import extra.nv_gpu_driver.nv_ioctl # noqa: F401 # pylint: disable=unused-import

def get_error_str(status): return f"{status}: {nv_gpu.nv_status_codes.get(status, 'Unknown error')}"

NV_PFAULT_FAULT_TYPE = {dt:name for name,dt in nv_gpu.__dict__.items() if name.startswith("NV_PFAULT_FAULT_TYPE_")}
NV_PFAULT_ACCESS_TYPE = {dt:name.split("_")[-1] for name,dt in nv_gpu.__dict__.items() if name.startswith("NV_PFAULT_ACCESS_TYPE_")}

def nv_iowr(fd:FileIOInterface, nr, args):
  ret = fd.ioctl((3 << 30) | (ctypes.sizeof(args) & 0x1FFF) << 16 | (ord('F') & 0xFF) << 8 | (nr & 0xFF), args)
  if ret != 0: raise RuntimeError(f"ioctl returned {ret}")

def rm_alloc(fd, clss, root, parant, params):
  made = nv_gpu.NVOS21_PARAMETERS(hRoot=root, hObjectParent=parant, hClass=clss,
                                  pAllocParms=ctypes.cast(ctypes.byref(params), ctypes.c_void_p) if params is not None else None)
  nv_iowr(fd, nv_gpu.NV_ESC_RM_ALLOC, made)
  if made.status != 0:
    if made.status == nv_gpu.NV_ERR_NO_MEMORY: raise MemoryError(f"rm_alloc returned {get_error_str(made.status)}")
    raise RuntimeError(f"rm_alloc returned {get_error_str(made.status)}")
  return made

def rm_control(cmd, sttyp, fd, client, obj, **kwargs):
  made = nv_gpu.NVOS54_PARAMETERS(hClient=client, hObject=obj, cmd=cmd, paramsSize=ctypes.sizeof(params:=sttyp(**kwargs)),
                                  params=ctypes.cast(ctypes.byref(params), ctypes.c_void_p) if params is not None else None)
  nv_iowr(fd, nv_gpu.NV_ESC_RM_CONTROL, made)
  if made.status != 0: raise RuntimeError(f"rm_control returned {get_error_str(made.status)}")
  return params

def make_rmctrl_type():
  return type("NVRMCTRL", (object,), {name[name.find("_CTRL_CMD_")+10:].lower(): functools.partial(rm_control, dt, sttyp)
    for name,dt in nv_gpu.__dict__.items() if name.find("_CTRL_CMD_")>=0 and (sttyp:=getattr(nv_gpu, name.replace("_CTRL_CMD_", "_CTRL_")+"_PARAMS", \
      getattr(nv_gpu, name+"_PARAMS", getattr(nv_gpu, name.replace("_CTRL_CMD_", "_CTRL_DEBUG_")+"_PARAMETERS", None))))})
rmctrl = make_rmctrl_type()

def uvm_ioctl(cmd, sttyp, fd:FileIOInterface, **kwargs):
  ret = fd.ioctl(cmd, made:=sttyp(**kwargs))
  if ret != 0: raise RuntimeError(f"ioctl(uvm) returned {ret}")
  if made.rmStatus != 0: raise RuntimeError(f"uvm_ioctl returned {get_error_str(made.rmStatus)}")
  return made

def make_uvm_type():
  return type("NVUVM", (object,), {name.replace("UVM_", "").lower(): functools.partial(uvm_ioctl, dt, getattr(nv_gpu, name+"_PARAMS"))
                                   for name,dt in nv_gpu.__dict__.items() if name.startswith("UVM_") and nv_gpu.__dict__.get(name+"_PARAMS")})
uvm = make_uvm_type()

class QMD:
  fields: dict[str, dict[str, tuple[int, int]]] = {}

  def __init__(self, dev:NVDevice, addr:int|None=None, **kwargs):
    self.ver, self.sz = (5, 0x60) if dev.compute_class >= nv_gpu.BLACKWELL_COMPUTE_A else (3, 0x40)

    # Init fields from module
    if (pref:="NVCEC0_QMDV05_00" if self.ver == 5 else "NVC6C0_QMDV03_00") not in QMD.fields:
      QMD.fields[pref] = {**{name[len(pref)+1:]: dt for name,dt in nv_gpu.__dict__.items() if name.startswith(pref) and isinstance(dt, tuple)},
        **{name[len(pref)+1:]+f"_{i}": dt(i) for name,dt in nv_gpu.__dict__.items() for i in range(8) if name.startswith(pref) and callable(dt)}}

    self.mv, self.pref = (memoryview(bytearray(self.sz * 4)) if addr is None else to_mv(addr, self.sz * 4)), pref
    if kwargs: self.write(**kwargs)

  def _rw_bits(self, hi:int, lo:int, value:int|None=None):
    mask = ((1 << (width:=hi - lo + 1)) - 1) << (lo % 8)
    num = int.from_bytes(self.mv[lo//8:hi//8+1], "little")

    if value is None: return (num & mask) >> (lo % 8)

    if value >= (1 << width): raise ValueError(f"{value:#x} does not fit.")
    self.mv[lo//8:hi//8+1] = int((num & ~mask) | ((value << (lo % 8)) & mask)).to_bytes((hi//8 - lo//8 + 1), "little")

  def write(self, **kwargs):
    for k,val in kwargs.items(): self._rw_bits(*QMD.fields[self.pref][k.upper()], value=val)

  def read(self, k, val=0): return self._rw_bits(*QMD.fields[self.pref][k.upper()])

  def field_offset(self, k): return QMD.fields[self.pref][k.upper()][1] // 8

  def set_constant_buf_addr(self, i, addr):
    if self.ver < 4: self.write(**{f'constant_buffer_addr_upper_{i}':hi32(addr), f'constant_buffer_addr_lower_{i}':lo32(addr)})
    else: self.write(**{f'constant_buffer_addr_upper_shifted6_{i}':hi32(addr >> 6), f'constant_buffer_addr_lower_shifted6_{i}':lo32(addr >> 6)})

class NVSignal(HCQSignal):
  def __init__(self, base_buf:HCQBuffer|None=None, **kwargs):
    super().__init__(base_buf, **kwargs, timestamp_divider=1000, dev_t=NVDevice)

class NVCommandQueue(HWQueue[NVSignal, 'NVDevice', 'NVProgram', 'NVArgsState']):
  def __init__(self):
    self.active_qmd = None
    super().__init__()

  def __del__(self):
    if self.binded_device is not None: self.binded_device.allocator.free(self.hw_page, self.hw_page.size, BufferSpec(cpu_access=True, nolru=True))

  def nvm(self, subchannel, mthd, *args, typ=2): self.q((typ << 28) | (len(args) << 16) | (subchannel << 13) | (mthd >> 2), *args)

  def setup(self, compute_class=None, copy_class=None, local_mem_window=None, shared_mem_window=None, local_mem=None, local_mem_tpc_bytes=None):
    if compute_class: self.nvm(1, nv_gpu.NVC6C0_SET_OBJECT, compute_class)
    if copy_class: self.nvm(4, nv_gpu.NVC6C0_SET_OBJECT, copy_class)
    if local_mem_window: self.nvm(1, nv_gpu.NVC6C0_SET_SHADER_LOCAL_MEMORY_WINDOW_A, *data64(local_mem_window))
    if shared_mem_window: self.nvm(1, nv_gpu.NVC6C0_SET_SHADER_SHARED_MEMORY_WINDOW_A, *data64(shared_mem_window))
    if local_mem: self.nvm(1, nv_gpu.NVC6C0_SET_SHADER_LOCAL_MEMORY_A, *data64(local_mem))
    if local_mem_tpc_bytes: self.nvm(1, nv_gpu.NVC6C0_SET_SHADER_LOCAL_MEMORY_NON_THROTTLED_A, *data64(local_mem_tpc_bytes), 0xff)
    return self

  def wait(self, signal:NVSignal, value:sint=0):
    self.nvm(0, nv_gpu.NVC56F_SEM_ADDR_LO, *data64_le(signal.value_addr), *data64_le(value), (3 << 0) | (1 << 24)) # ACQUIRE | PAYLOAD_SIZE_64BIT
    self.active_qmd = None
    return self

  def timestamp(self, signal:NVSignal): return self.signal(signal, 0)

  def bind(self, dev:NVDevice):
    self.binded_device = dev
    self.hw_page = dev.allocator.alloc(len(self._q) * 4, BufferSpec(cpu_access=True, nolru=True))
    hw_view = self.hw_page.cpu_view().view(fmt='I')
    for i, value in enumerate(self._q): hw_view[i] = value

    # From now on, the queue is on the device for faster submission.
    self._q = hw_view

  def _submit_to_gpfifo(self, dev:NVDevice, gpfifo:GPFifo):
    if dev == self.binded_device: cmdq_addr = self.hw_page.va_addr
    else:
      cmdq_addr = dev.cmdq_allocator.alloc(len(self._q) * 4)
      cmdq_wptr = (cmdq_addr - dev.cmdq_page.va_addr) // 4
      dev.cmdq[cmdq_wptr : cmdq_wptr + len(self._q)] = array.array('I', self._q)

    gpfifo.ring[gpfifo.put_value % gpfifo.entries_count] = (cmdq_addr//4 << 2) | (len(self._q) << 42) | (1 << 41)
    gpfifo.controls.GPPut = (gpfifo.put_value + 1) % gpfifo.entries_count

    if CPUProgram.atomic_lib is not None: CPUProgram.atomic_lib.atomic_thread_fence(__ATOMIC_SEQ_CST:=5)
    dev.gpu_mmio[0x90 // 4] = gpfifo.token
    gpfifo.put_value += 1

class NVComputeQueue(NVCommandQueue):
  def memory_barrier(self):
    self.nvm(1, nv_gpu.NVC6C0_INVALIDATE_SHADER_CACHES_NO_WFI, (1 << 12) | (1 << 4) | (1 << 0))
    self.active_qmd:QMD|None = None
    return self

  def exec(self, prg:NVProgram, args_state:NVArgsState, global_size:tuple[sint, ...], local_size:tuple[sint, ...]):
    self.bind_args_state(args_state)

    qmd_buf = args_state.buf.offset(round_up(prg.constbufs[0][1], 1 << 8))
    qmd_buf.cpu_view().view(size=prg.qmd.mv.nbytes, fmt='B')[:] = prg.qmd.mv
    assert qmd_buf.va_addr < (1 << 40), f"large qmd addr {qmd_buf.va_addr:x}"

    qmd = QMD(dev=prg.dev, addr=cast(int, qmd_buf.va_addr)) # Save qmd for later update

    self.bind_sints_to_mem(*global_size, mem=qmd_buf.cpu_view(), fmt='I', offset=qmd.field_offset('cta_raster_width' if qmd.ver<4 else 'grid_width'))
    self.bind_sints_to_mem(*(local_size[:2]), mem=qmd_buf.cpu_view(), fmt='H', offset=qmd.field_offset('cta_thread_dimension0'))
    self.bind_sints_to_mem(local_size[2], mem=qmd_buf.cpu_view(), fmt='B', offset=qmd.field_offset('cta_thread_dimension2'))
    qmd.set_constant_buf_addr(0, args_state.buf.va_addr)

    if self.active_qmd is None:
      self.nvm(1, nv_gpu.NVC6C0_SEND_PCAS_A, qmd_buf.va_addr >> 8)
      self.nvm(1, nv_gpu.NVC6C0_SEND_SIGNALING_PCAS2_B, 9)
    else:
      self.active_qmd.write(dependent_qmd0_pointer=qmd_buf.va_addr >> 8, dependent_qmd0_action=1, dependent_qmd0_prefetch=1, dependent_qmd0_enable=1)

    self.active_qmd, self.active_qmd_buf = qmd, qmd_buf
    return self

  def signal(self, signal:NVSignal, value:sint=0):
    if self.active_qmd is not None:
      for i in range(2):
        if self.active_qmd.read(f'release{i}_enable') == 0:
          self.active_qmd.write(**{f'release{i}_enable': 1})
          self.bind_sints_to_mem(signal.value_addr, mem=self.active_qmd_buf.cpu_view(), fmt='Q', mask=0xfffffffff,
            offset=self.active_qmd.field_offset(f'release{i}_address_lower' if self.active_qmd.ver<4 else f'release_semaphore{i}_addr_lower'))
          self.bind_sints_to_mem(value, mem=self.active_qmd_buf.cpu_view(), fmt='Q',
            offset=self.active_qmd.field_offset(f'release{i}_payload_lower' if self.active_qmd.ver<4 else f'release_semaphore{i}_payload_lower'))
          return self

    self.nvm(0, nv_gpu.NVC56F_SEM_ADDR_LO, *data64_le(signal.value_addr), *data64_le(value),
             (1 << 0) | (1 << 20) | (1 << 24) | (1 << 25)) # RELEASE | RELEASE_WFI | PAYLOAD_SIZE_64BIT | RELEASE_TIMESTAMP
    self.nvm(0, nv_gpu.NVC56F_NON_STALL_INTERRUPT, 0x0)
    self.active_qmd = None
    return self

  def _submit(self, dev:NVDevice): self._submit_to_gpfifo(dev, dev.compute_gpfifo)

class NVCopyQueue(NVCommandQueue):
  def copy(self, dest:sint, src:sint, copy_size:int):
    for off in range(0, copy_size, step:=(1 << 31)):
      self.nvm(4, nv_gpu.NVC6B5_OFFSET_IN_UPPER, *data64(src+off), *data64(dest+off))
      self.nvm(4, nv_gpu.NVC6B5_LINE_LENGTH_IN, min(copy_size-off, step))
      self.nvm(4, nv_gpu.NVC6B5_LAUNCH_DMA, 0x182) # TRANSFER_TYPE_NON_PIPELINED | DST_MEMORY_LAYOUT_PITCH | SRC_MEMORY_LAYOUT_PITCH
    return self

  def signal(self, signal:NVSignal, value:sint=0):
    self.nvm(4, nv_gpu.NVC6B5_SET_SEMAPHORE_A, *data64(signal.value_addr), value)
    self.nvm(4, nv_gpu.NVC6B5_LAUNCH_DMA, 0x14)
    return self

  def _submit(self, dev:NVDevice): self._submit_to_gpfifo(dev, dev.dma_gpfifo)

class NVArgsState(CLikeArgsState):
  def __init__(self, buf:HCQBuffer, prg:NVProgram, bufs:tuple[HCQBuffer, ...], vals:tuple[int, ...]=()):
    if MOCKGPU: prg.constbuffer_0[80:82] = [len(bufs), len(vals)]
    super().__init__(buf, prg, bufs, vals=vals, prefix=prg.constbuffer_0)

class NVProgram(HCQProgram):
  def __init__(self, dev:NVDevice, name:str, lib:bytes):
    self.dev, self.name, self.lib = dev, name, lib

    # For MOCKGPU, the lib is PTX code, so some values are emulated.
    cbuf0_size = 0 if not MOCKGPU else 0x160

    if MOCKGPU: image, sections, relocs = memoryview(bytearray(lib) + b'\x00' * (4 - len(lib)%4)).cast("I"), [], [] # type: ignore
    else: image, sections, relocs = elf_loader(self.lib, force_section_align=128)

    # NOTE: Ensure at least 4KB of space after the program to mitigate prefetch memory faults.
    self.lib_gpu = self.dev.allocator.alloc(round_up(image.nbytes, 0x1000) + 0x1000, buf_spec:=BufferSpec(cpu_access=True))

    self.prog_addr, self.prog_sz, self.regs_usage, self.shmem_usage, self.lcmem_usage = self.lib_gpu.va_addr, image.nbytes, 0, 0x400, 0
    self.constbufs: dict[int, tuple[int, int]] = {0: (0, 0x160)} # dict[constbuf index, tuple[va_addr, size]]
    for sh in sections:
      if sh.name == f".nv.shared.{self.name}": self.shmem_usage = round_up(0x400 + sh.header.sh_size, 128)
      if sh.name == f".text.{self.name}": self.prog_addr, self.prog_sz = self.lib_gpu.va_addr+sh.header.sh_addr, sh.header.sh_size
      elif m:=re.match(r'\.nv\.constant(\d+)', sh.name): self.constbufs[int(m.group(1))] = (self.lib_gpu.va_addr+sh.header.sh_addr, sh.header.sh_size)
      elif sh.name.startswith(".nv.info"):
        for typ, param, data in self._parse_elf_info(sh):
          if sh.name == f".nv.info.{name}" and param == 0xa: cbuf0_size = struct.unpack_from("IH", data)[1] # EIATTR_PARAM_CBANK
          elif sh.name == ".nv.info" and param == 0x12: self.lcmem_usage = struct.unpack_from("II", data)[1] + 0x240 # EIATTR_MIN_STACK_SIZE
          elif sh.name == ".nv.info" and param == 0x2f: self.regs_usage = struct.unpack_from("II", data)[1] # EIATTR_REGCOUNT

    # Ensure device has enough local memory to run the program
    self.dev._ensure_has_local_memory(self.lcmem_usage)

    # Apply relocs
    for apply_image_offset, rel_sym_offset, typ, _ in relocs:
      # These types are CUDA-specific, applying them here
      if typ == 2: image[apply_image_offset:apply_image_offset+8] = struct.pack('<Q', self.lib_gpu.va_addr + rel_sym_offset) # R_CUDA_64
      elif typ == 0x38: image[apply_image_offset+4:apply_image_offset+8] = struct.pack('<I', (self.lib_gpu.va_addr + rel_sym_offset) & 0xffffffff)
      elif typ == 0x39: image[apply_image_offset+4:apply_image_offset+8] = struct.pack('<I', (self.lib_gpu.va_addr + rel_sym_offset) >> 32)
      else: raise RuntimeError(f"unknown NV reloc {typ}")

    ctypes.memmove(self.lib_gpu.va_addr, mv_address(image), image.nbytes)

    self.constbuffer_0 = [0] * (cbuf0_size // 4)

    if dev.compute_class >= nv_gpu.BLACKWELL_COMPUTE_A:
      self.constbuffer_0[188:192], self.constbuffer_0[223] = [*data64_le(self.dev.shared_mem_window), *data64_le(self.dev.local_mem_window)], 0xfffdc0
      qmd = {'qmd_major_version':5, 'qmd_type':nv_gpu.NVCEC0_QMDV05_00_QMD_TYPE_GRID_CTA, 'register_count':self.regs_usage,
        'program_address_upper_shifted4':hi32(self.prog_addr>>4), 'program_address_lower_shifted4':lo32(self.prog_addr>>4),
        'shared_memory_size_shifted7':self.shmem_usage>>7, 'shader_local_memory_high_size_shifted4':self.dev.slm_per_thread>>4}
    else:
      self.constbuffer_0[6:12] = [*data64_le(self.dev.shared_mem_window), *data64_le(self.dev.local_mem_window), *data64_le(0xfffdc0)]
      qmd = {'qmd_major_version':3, 'sm_global_caching_enable':1, 'shader_local_memory_high_size':self.dev.slm_per_thread,
        'program_address_upper':hi32(self.prog_addr), 'program_address_lower':lo32(self.prog_addr), 'shared_memory_size':self.shmem_usage,
        'register_count_v':self.regs_usage}

    smem_cfg = min(shmem_conf * 1024 for shmem_conf in [32, 64, 100] if shmem_conf * 1024 >= self.shmem_usage) // 4096 + 1

    self.qmd:QMD = QMD(dev, **qmd, qmd_group_id=0x3f, invalidate_texture_header_cache=1, invalidate_texture_sampler_cache=1,
      invalidate_texture_data_cache=1, invalidate_shader_data_cache=1, api_visible_call_limit=1, sampler_index=1, barrier_count=1,
      cwd_membar_type=nv_gpu.NVC6C0_QMDV03_00_CWD_MEMBAR_TYPE_L1_SYSMEMBAR, constant_buffer_invalidate_0=1,
      min_sm_config_shared_mem_size=smem_cfg, target_sm_config_shared_mem_size=smem_cfg, max_sm_config_shared_mem_size=0x1a,
      program_prefetch_size=min(self.prog_sz>>8, 0x1ff), sass_version=dev.sass_version,
      program_prefetch_addr_upper_shifted=self.prog_addr>>40, program_prefetch_addr_lower_shifted=self.prog_addr>>8)

    for i,(addr,sz) in self.constbufs.items():
      self.qmd.set_constant_buf_addr(i, addr)
      self.qmd.write(**{f'constant_buffer_size_shifted4_{i}': sz, f'constant_buffer_valid_{i}': 1})

    # Registers allocation granularity per warp is 256, warp allocation granularity is 4. Register file size is 65536.
    self.max_threads = ((65536 // round_up(max(1, self.regs_usage) * 32, 256)) // 4) * 4 * 32

    # NV's kernargs is constbuffer, then arguments to the kernel follows. Kernargs also appends QMD at the end of the kernel.
    super().__init__(NVArgsState, self.dev, self.name, kernargs_alloc_size=round_up(self.constbufs[0][1], 1 << 8) + (8 << 8))
    weakref.finalize(self, self._fini, self.dev, self.lib_gpu, buf_spec)

  def _parse_elf_info(self, sh, start_off=0):
    while start_off < sh.header.sh_size:
      typ, param, sz = struct.unpack_from("BBH", sh.content, start_off)
      yield typ, param, sh.content[start_off+4:start_off+sz+4] if typ == 0x4 else sz
      start_off += (sz if typ == 0x4 else 0) + 4

  def __call__(self, *bufs, global_size:tuple[int,int,int]=(1,1,1), local_size:tuple[int,int,int]=(1,1,1), vals:tuple[int, ...]=(), wait=False):
    if prod(local_size) > 1024 or self.max_threads < prod(local_size) or self.lcmem_usage > cast(NVDevice, self.dev).slm_per_thread:
      raise RuntimeError(f"Too many resources requested for launch, {prod(local_size)=}, {self.max_threads=}")
    if any(cur > mx for cur,mx in zip(global_size, [2147483647, 65535, 65535])) or any(cur > mx for cur,mx in zip(local_size, [1024, 1024, 64])):
      raise RuntimeError(f"Invalid global/local dims {global_size=}, {local_size=}")
    return super().__call__(*bufs, global_size=global_size, local_size=local_size, vals=vals, wait=wait)

class NVAllocator(HCQAllocator['NVDevice']):
  def _alloc(self, size:int, options:BufferSpec) -> HCQBuffer:
    if options.host: return self.dev._gpu_alloc(size, host=True, tag="user host memory")
    return self.dev._gpu_alloc(size, cpu_access=options.cpu_access, tag=f"user memory ({options})")

  def _free(self, opaque:HCQBuffer, options:BufferSpec):
    try:
      self.dev.synchronize()
      self.dev._gpu_free(opaque)
    except AttributeError: pass

  def map(self, buf:HCQBuffer): self.dev._gpu_map(buf._base if buf._base is not None else buf)

@dataclass
class GPFifo:
  ring: MMIOInterface
  controls: nv_gpu.AmpereAControlGPFifo
  entries_count: int
  token: int
  put_value: int = 0

MAP_FIXED, MAP_NORESERVE = 0x10, 0x400
class NVDevice(HCQCompiled[NVSignal]):
  devices: ClassVar[list[HCQCompiled]] = []
  signal_pages: ClassVar[list[HCQBuffer]] = []
  signal_pool: ClassVar[list[HCQBuffer]] = []

  root = None
  fd_ctl: FileIOInterface
  fd_uvm: FileIOInterface
  gpus_info: Union[list, ctypes.Array] = []

  # TODO: Need a proper allocator for va addresses
  # 0x1000000000 - 0x2000000000, reserved for system/cpu mappings
  # VA space is 48bits.
  low_uvm_vaddr_allocator: BumpAllocator = BumpAllocator(size=0x1000000000, base=0x8000000000 if OSX else 0x1000000000, wrap=False)
  uvm_vaddr_allocator: BumpAllocator = BumpAllocator(size=(1 << 48) - 1, base=low_uvm_vaddr_allocator.base + low_uvm_vaddr_allocator.size, wrap=False)
  host_object_enumerator: int = 0x1000

  def _new_gpu_fd(self):
    fd_dev = FileIOInterface(f"/dev/nvidia{NVDevice.gpus_info[self.device_id].minor_number}", os.O_RDWR | os.O_CLOEXEC)
    nv_iowr(fd_dev, nv_gpu.NV_ESC_REGISTER_FD, nv_gpu.nv_ioctl_register_fd_t(ctl_fd=self.fd_ctl.fd))
    return fd_dev

  def _gpu_map_to_cpu(self, memory_handle, size, target=None, flags=0, system=False):
    fd_dev = self._new_gpu_fd() if not system else FileIOInterface("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
    made = nv_gpu.nv_ioctl_nvos33_parameters_with_fd(fd=fd_dev.fd,
      params=nv_gpu.NVOS33_PARAMETERS(hClient=self.root, hDevice=self.nvdevice, hMemory=memory_handle, length=size, flags=flags))
    nv_iowr(self.fd_ctl, nv_gpu.NV_ESC_RM_MAP_MEMORY, made)
    if made.params.status != 0: raise RuntimeError(f"_gpu_map_to_cpu returned {get_error_str(made.params.status)}")
    return fd_dev.mmap(target, size, mmap.PROT_READ|mmap.PROT_WRITE, mmap.MAP_SHARED | (MAP_FIXED if target is not None else 0), 0)

  def _gpu_alloc(self, size:int, host=False, uncached=False, cpu_access=False, contiguous=False, map_flags=0, tag="") -> HCQBuffer:
    # Uncached memory is "system". Use huge pages only for gpu memory.
    page_size = (4 << (12 if OSX else 10)) if uncached or host else ((2 << 20) if size >= (8 << 20) else (4 << (12 if OSX else 10)))
    size = round_up(size, page_size)
    va_addr = self._alloc_gpu_vaddr(size, alignment=page_size, force_low=cpu_access)

    if host:
      va_addr = FileIOInterface.anon_mmap(va_addr, size, mmap.PROT_READ | mmap.PROT_WRITE, MAP_FIXED | mmap.MAP_SHARED | mmap.MAP_ANONYMOUS, 0)

      flags = (nv_gpu.NVOS02_FLAGS_PHYSICALITY_NONCONTIGUOUS << 4) | (nv_gpu.NVOS02_FLAGS_COHERENCY_CACHED << 12) \
            | (nv_gpu.NVOS02_FLAGS_MAPPING_NO_MAP << 30)

      NVDevice.host_object_enumerator += 1
      made = nv_gpu.nv_ioctl_nvos02_parameters_with_fd(params=nv_gpu.NVOS02_PARAMETERS(hRoot=self.root, hObjectParent=self.nvdevice, flags=flags,
        hObjectNew=NVDevice.host_object_enumerator, hClass=nv_gpu.NV01_MEMORY_SYSTEM_OS_DESCRIPTOR, pMemory=va_addr, limit=size-1), fd=-1)
      nv_iowr(self.fd_dev, nv_gpu.NV_ESC_RM_ALLOC_MEMORY, made)

      if made.params.status != 0: raise RuntimeError(f"host alloc returned {get_error_str(made.params.status)}")
      mem_handle = made.params.hObjectNew
    else:
      attr = ((nv_gpu.NVOS32_ATTR_PHYSICALITY_CONTIGUOUS if contiguous else nv_gpu.NVOS32_ATTR_PHYSICALITY_ALLOW_NONCONTIGUOUS) << 27) \
          | (nv_gpu.NVOS32_ATTR_PAGE_SIZE_HUGE if page_size > 0x1000 else 0) << 23 | ((nv_gpu.NVOS32_ATTR_LOCATION_PCI if uncached else 0) << 25)

      attr2 = ((nv_gpu.NVOS32_ATTR2_GPU_CACHEABLE_NO if uncached else nv_gpu.NVOS32_ATTR2_GPU_CACHEABLE_YES) << 2) \
            | ((nv_gpu.NVOS32_ATTR2_PAGE_SIZE_HUGE_2MB if page_size > 0x1000 else 0) << 20) | nv_gpu.NVOS32_ATTR2_ZBC_PREFER_NO_ZBC

      fl = nv_gpu.NVOS32_ALLOC_FLAGS_MAP_NOT_REQUIRED | nv_gpu.NVOS32_ALLOC_FLAGS_MEMORY_HANDLE_PROVIDED | nv_gpu.NVOS32_ALLOC_FLAGS_ALIGNMENT_FORCE \
         | nv_gpu.NVOS32_ALLOC_FLAGS_IGNORE_BANK_PLACEMENT | (nv_gpu.NVOS32_ALLOC_FLAGS_PERSISTENT_VIDMEM if not uncached else 0)

      alloc_func = nv_gpu.NV1_MEMORY_SYSTEM if uncached else nv_gpu.NV1_MEMORY_USER
      alloc_params = nv_gpu.NV_MEMORY_ALLOCATION_PARAMS(owner=self.root, alignment=page_size, offset=0, limit=size-1, format=6, size=size,
        type=nv_gpu.NVOS32_TYPE_NOTIFIER if uncached else nv_gpu.NVOS32_TYPE_IMAGE, attr=attr, attr2=attr2, flags=fl)
      mem_handle = rm_alloc(self.fd_ctl, alloc_func, self.root, self.nvdevice, alloc_params).hObjectNew

      if cpu_access: va_addr = self._gpu_map_to_cpu(mem_handle, size, target=va_addr, flags=map_flags, system=uncached)

    return self._gpu_uvm_map(va_addr, size, mem_handle, has_cpu_mapping=cpu_access or host, tag=tag)

  def _gpu_free(self, mem:HCQBuffer):
    if mem.meta.hMemory > NVDevice.host_object_enumerator: # not a host object, clear phys mem.
      made = nv_gpu.NVOS00_PARAMETERS(hRoot=self.root, hObjectParent=self.nvdevice, hObjectOld=mem.meta.hMemory)
      nv_iowr(self.fd_ctl, nv_gpu.NV_ESC_RM_FREE, made)
      if made.status != 0: raise RuntimeError(f"_gpu_free returned {get_error_str(made.status)}")

    self._debug_mappings.pop((cast(int, mem.va_addr), mem.size))
    uvm.free(self.fd_uvm, base=cast(int, mem.va_addr), length=mem.size)
    if mem.meta.has_cpu_mapping: FileIOInterface.munmap(cast(int, mem.va_addr), mem.size)

  def _gpu_uvm_map(self, va_base, size, mem_handle, create_range=True, has_cpu_mapping=False, tag="") -> HCQBuffer:
    if create_range: uvm.create_external_range(self.fd_uvm, base=va_base, length=size)
    attrs = (nv_gpu.struct_c__SA_UvmGpuMappingAttributes*256)(nv_gpu.struct_c__SA_UvmGpuMappingAttributes(gpuUuid=self.gpu_uuid, gpuMappingType=1))

    # NOTE: va_addr is set to make rawbufs compatible with HCQBuffer protocol.
    self._debug_mappings[(va_base, size)] = tag
    return HCQBuffer(va_base, size, meta=uvm.map_external_allocation(self.fd_uvm, base=va_base, length=size, rmCtrlFd=self.fd_ctl.fd,
      hClient=self.root, hMemory=mem_handle, gpuAttributesCount=1, perGpuAttributes=attrs,
      mapped_gpu_ids=[self.gpu_uuid], has_cpu_mapping=has_cpu_mapping),
      view=MMIOInterface(va_base, size, fmt='B') if has_cpu_mapping else None)

  def _gpu_map(self, mem:HCQBuffer):
    if self.gpu_uuid in mem.meta.mapped_gpu_ids: return
    mem.meta.mapped_gpu_ids.append(self.gpu_uuid)
    self._gpu_uvm_map(mem.va_addr, mem.size, mem.meta.hMemory, create_range=False, tag="p2p mem")

  def _alloc_gpu_vaddr(self, size, alignment=(4 << 10), force_low=False):
    return NVDevice.low_uvm_vaddr_allocator.alloc(size, alignment) if force_low else NVDevice.uvm_vaddr_allocator.alloc(size, alignment)

  def _setup_nvclasses(self):
    classlist = memoryview(bytearray(100 * 4)).cast('I')
    clsinfo = rmctrl.gpu_get_classlist(self.fd_ctl, self.root, self.nvdevice, numClasses=100, classList=mv_address(classlist))
    self.nvclasses = {classlist[i] for i in range(clsinfo.numClasses)}
    self.usermode_class:int = next(c for c in [nv_gpu.HOPPER_USERMODE_A, nv_gpu.TURING_USERMODE_A] if c in self.nvclasses)
    self.gpfifo_class:int = next(c for c in [nv_gpu.BLACKWELL_CHANNEL_GPFIFO_A, nv_gpu.AMPERE_CHANNEL_GPFIFO_A] if c in self.nvclasses)
    self.compute_class:int = next(c for c in [nv_gpu.BLACKWELL_COMPUTE_B, nv_gpu.ADA_COMPUTE_A, nv_gpu.AMPERE_COMPUTE_B] if c in self.nvclasses)
    self.dma_class:int = next(c for c in [nv_gpu.BLACKWELL_DMA_COPY_B, nv_gpu.AMPERE_DMA_COPY_B] if c in self.nvclasses)

  def __init__(self, device:str=""):
    if NVDevice.root is None:
      NVDevice.fd_ctl = FileIOInterface("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
      NVDevice.fd_uvm = FileIOInterface("/dev/nvidia-uvm", os.O_RDWR | os.O_CLOEXEC)
      self.fd_uvm_2 = FileIOInterface("/dev/nvidia-uvm", os.O_RDWR | os.O_CLOEXEC)
      NVDevice.root = rm_alloc(self.fd_ctl, nv_gpu.NV01_ROOT_CLIENT, 0, 0, None).hObjectNew
      uvm.initialize(self.fd_uvm)
      with contextlib.suppress(RuntimeError): uvm.mm_initialize(self.fd_uvm_2, uvmFd=self.fd_uvm.fd) # this error is okay, CUDA hits it too

      nv_iowr(NVDevice.fd_ctl, nv_gpu.NV_ESC_CARD_INFO, gpus_info:=(nv_gpu.nv_ioctl_card_info_t*64)())
      visible_devices = [int(x) for x in (getenv('VISIBLE_DEVICES', getenv('CUDA_VISIBLE_DEVICES', ''))).split(',') if x.strip()]
      NVDevice.gpus_info = [gpus_info[x] for x in visible_devices] if visible_devices else gpus_info

    self.device_id = int(device.split(":")[1]) if ":" in device else 0

    if self.device_id >= len(NVDevice.gpus_info) or not NVDevice.gpus_info[self.device_id].valid:
      raise RuntimeError(f"No device found for {device}. Requesting more devices than the system has?")

    self.fd_dev = self._new_gpu_fd()
    self.gpu_info = rmctrl.gpu_get_id_info_v2(self.fd_ctl, self.root, self.root, gpuId=NVDevice.gpus_info[self.device_id].gpu_id)
    self.gpu_minor = NVDevice.gpus_info[self.device_id].minor_number

    device_params = nv_gpu.NV0080_ALLOC_PARAMETERS(deviceId=self.gpu_info.deviceInstance, hClientShare=self.root,
                                                   vaMode=nv_gpu.NV_DEVICE_ALLOCATION_VAMODE_MULTIPLE_VASPACES)
    self.nvdevice = rm_alloc(self.fd_ctl, nv_gpu.NV01_DEVICE_0, self.root, self.root, device_params).hObjectNew
    self.subdevice = rm_alloc(self.fd_ctl, nv_gpu.NV20_SUBDEVICE_0, self.root, self.nvdevice, None).hObjectNew

    self._setup_nvclasses()
    self._debug_mappings: dict[tuple[int, int], str] = dict()

    self.usermode = rm_alloc(self.fd_ctl, self.usermode_class, self.root, self.subdevice, None).hObjectNew
    self.gpu_mmio = MMIOInterface(self._gpu_map_to_cpu(self.usermode, mmio_sz:=0x10000, flags=2), mmio_sz, fmt='I')

    rmctrl.perf_boost(self.fd_ctl, self.root, self.subdevice, duration=0xffffffff, flags=((nv_gpu.NV2080_CTRL_PERF_BOOST_FLAGS_CUDA_YES << 4) | \
      (nv_gpu.NV2080_CTRL_PERF_BOOST_FLAGS_CUDA_PRIORITY_HIGH << 6) | (nv_gpu.NV2080_CTRL_PERF_BOOST_FLAGS_CMD_BOOST_TO_MAX << 0)))

    vaspace_params = nv_gpu.NV_VASPACE_ALLOCATION_PARAMETERS(vaBase=0x1000, vaSize=0x1fffffb000000,
      flags=nv_gpu.NV_VASPACE_ALLOCATION_FLAGS_ENABLE_PAGE_FAULTING | nv_gpu.NV_VASPACE_ALLOCATION_FLAGS_IS_EXTERNALLY_OWNED)
    vaspace = rm_alloc(self.fd_ctl, nv_gpu.FERMI_VASPACE_A, self.root, self.nvdevice, vaspace_params).hObjectNew

    raw_uuid = rmctrl.gpu_get_gid_info(self.fd_ctl, self.root, self.subdevice, flags=nv_gpu.NV2080_GPU_CMD_GPU_GET_GID_FLAGS_FORMAT_BINARY, length=16)
    self.gpu_uuid = nv_gpu.struct_nv_uuid(uuid=(ctypes.c_ubyte*16)(*[raw_uuid.data[i] for i in range(16)]))

    uvm.register_gpu(self.fd_uvm, rmCtrlFd=-1, gpu_uuid=self.gpu_uuid)
    uvm.register_gpu_vaspace(self.fd_uvm, gpuUuid=self.gpu_uuid, rmCtrlFd=self.fd_ctl.fd, hClient=self.root, hVaSpace=vaspace)

    for dev in cast(list[NVDevice], self.devices):
      try: uvm.enable_peer_access(self.fd_uvm, gpuUuidA=self.gpu_uuid, gpuUuidB=dev.gpu_uuid)
      except RuntimeError as e: raise RuntimeError(str(e) + f". Make sure GPUs #{self.gpu_minor} & #{dev.gpu_minor} have P2P enabled between.") from e

    channel_params = nv_gpu.NV_CHANNEL_GROUP_ALLOCATION_PARAMETERS(engineType=nv_gpu.NV2080_ENGINE_TYPE_GRAPHICS)
    channel_group = rm_alloc(self.fd_ctl, nv_gpu.KEPLER_CHANNEL_GROUP_A, self.root, self.nvdevice, channel_params).hObjectNew

    gpfifo_area = self._gpu_alloc(0x200000, contiguous=True, cpu_access=True, map_flags=0x10d0000, tag="gpfifo")

    ctxshare_params = nv_gpu.NV_CTXSHARE_ALLOCATION_PARAMETERS(hVASpace=vaspace, flags=nv_gpu.NV_CTXSHARE_ALLOCATION_FLAGS_SUBCONTEXT_ASYNC)
    ctxshare = rm_alloc(self.fd_ctl, nv_gpu.FERMI_CONTEXT_SHARE_A, self.root, channel_group, ctxshare_params).hObjectNew

    self.compute_gpfifo = self._new_gpu_fifo(gpfifo_area, ctxshare, channel_group, offset=0, entries=0x10000, enable_debug=True)
    self.dma_gpfifo = self._new_gpu_fifo(gpfifo_area, ctxshare, channel_group, offset=0x100000, entries=0x10000)

    rmctrl.gpfifo_schedule(self.fd_ctl, self.root, channel_group, bEnable=1)

    self.cmdq_page:HCQBuffer = self._gpu_alloc(0x200000, cpu_access=True, tag="cmdq")
    self.cmdq_allocator = BumpAllocator(size=self.cmdq_page.size, base=cast(int, self.cmdq_page.va_addr), wrap=True)
    self.cmdq = MMIOInterface(cast(int, self.cmdq_page.va_addr), 0x200000, fmt='I')

    self.num_gpcs, self.num_tpc_per_gpc, self.num_sm_per_tpc, self.max_warps_per_sm, self.sm_version = self._query_gpu_info('num_gpcs',
      'num_tpc_per_gpc', 'num_sm_per_tpc', 'max_warps_per_sm', 'sm_version')

    # FIXME: no idea how to convert this for blackwells
    self.arch: str = "sm_120" if self.sm_version==0xa04 else f"sm_{(self.sm_version>>8)&0xff}{(val>>4) if (val:=self.sm_version&0xff) > 0xf else val}"
    self.sass_version = ((self.sm_version & 0xf00) >> 4) | (self.sm_version & 0xf)

    compiler_t = (PTXCompiler if PTX else CUDACompiler) if MOCKGPU else (NVPTXCompiler if PTX else NVCompiler)
    super().__init__(device, NVAllocator(self), PTXRenderer(self.arch, device="NV") if PTX else NVRenderer(self.arch), compiler_t(self.arch),
                     functools.partial(NVProgram, self), NVSignal, NVComputeQueue, NVCopyQueue)

    self._setup_gpfifos()

  def _new_gpu_fifo(self, gpfifo_area, ctxshare, channel_group, offset=0, entries=0x400, enable_debug=False) -> GPFifo:
    notifier = self._gpu_alloc(48 << 20, uncached=True)
    params = nv_gpu.NV_CHANNELGPFIFO_ALLOCATION_PARAMETERS(hObjectError=notifier.meta.hMemory, hObjectBuffer=gpfifo_area.meta.hMemory,
      gpFifoOffset=gpfifo_area.va_addr+offset, gpFifoEntries=entries, hContextShare=ctxshare,
      hUserdMemory=(ctypes.c_uint32*8)(gpfifo_area.meta.hMemory), userdOffset=(ctypes.c_uint64*8)(entries*8+offset))
    gpfifo = rm_alloc(self.fd_ctl, self.gpfifo_class, self.root, channel_group, params).hObjectNew
    comp = rm_alloc(self.fd_ctl, self.compute_class, self.root, gpfifo, None).hObjectNew
    rm_alloc(self.fd_ctl, self.dma_class, self.root, gpfifo, None)

    if enable_debug:
      self.debug_compute_obj, self.debug_channel = comp, gpfifo
      debugger_params = nv_gpu.NV83DE_ALLOC_PARAMETERS(hAppClient=self.root, hClass3dObject=self.debug_compute_obj)
      self.debugger = rm_alloc(self.fd_ctl, nv_gpu.GT200_DEBUGGER, self.root, self.nvdevice, debugger_params).hObjectNew

    ws_token_params = rmctrl.gpfifo_get_work_submit_token(self.fd_ctl, self.root, gpfifo, workSubmitToken=-1)
    assert ws_token_params.workSubmitToken != -1

    channel_base = self._alloc_gpu_vaddr(0x4000000, force_low=True)
    uvm.register_channel(self.fd_uvm, gpuUuid=self.gpu_uuid, rmCtrlFd=self.fd_ctl.fd, hClient=self.root,
                         hChannel=gpfifo, base=channel_base, length=0x4000000)

    return GPFifo(ring=MMIOInterface(gpfifo_area.va_addr + offset, entries*8, fmt='Q'), entries_count=entries, token=ws_token_params.workSubmitToken,
                  controls=nv_gpu.AmpereAControlGPFifo.from_address(gpfifo_area.va_addr + offset + entries * 8))

  def _query_gpu_info(self, *reqs):
    nvrs = [getattr(nv_gpu,'NV2080_CTRL_GR_INFO_INDEX_'+r.upper(), getattr(nv_gpu,'NV2080_CTRL_GR_INFO_INDEX_LITTER_'+r.upper(),None)) for r in reqs]
    infos = (nv_gpu.NV2080_CTRL_GR_INFO*len(nvrs))(*[nv_gpu.NV2080_CTRL_GR_INFO(index=nvr) for nvr in nvrs])
    rmctrl.gr_get_info(self.fd_ctl, self.root, self.subdevice, grInfoListSize=len(infos), grInfoList=ctypes.addressof(infos))
    return [x.data for x in infos]

  def _setup_gpfifos(self):
    self.slm_per_thread, self.shader_local_mem = 0, None

    # Set windows addresses to not collide with other allocated buffers.
    self.shared_mem_window = 0x729400000000 if self.compute_class >= nv_gpu.BLACKWELL_COMPUTE_A else 0xfe000000
    self.local_mem_window = 0x729300000000 if self.compute_class >= nv_gpu.BLACKWELL_COMPUTE_A else 0xff000000

    NVComputeQueue().setup(compute_class=self.compute_class, local_mem_window=self.local_mem_window, shared_mem_window=self.shared_mem_window) \
                    .signal(self.timeline_signal, self.timeline_value).submit(self)

    cast(NVCopyQueue, NVCopyQueue().wait(self.timeline_signal, self.timeline_value)) \
                                   .setup(copy_class=self.dma_class) \
                                   .signal(self.timeline_signal, self.timeline_value + 1).submit(self)

    self.timeline_value += 2

  def _ensure_has_local_memory(self, required):
    if self.slm_per_thread >= required or ((maxlm:=getenv("NV_MAX_LOCAL_MEMORY_PER_THREAD")) > 0 and required >= maxlm): return

    self.slm_per_thread, old_slm_per_thread = round_up(required, 32), self.slm_per_thread
    bytes_per_tpc = round_up(round_up(self.slm_per_thread * 32, 0x200) * self.max_warps_per_sm * self.num_sm_per_tpc, 0x8000)
    self.shader_local_mem, ok = self._realloc(self.shader_local_mem, round_up(bytes_per_tpc*self.num_tpc_per_gpc*self.num_gpcs, 0x20000))

    # Realloc failed, restore the old value.
    if not ok: self.slm_per_thread = old_slm_per_thread

    cast(NVComputeQueue, NVComputeQueue().wait(self.timeline_signal, self.timeline_value - 1)) \
                                         .setup(local_mem=self.shader_local_mem.va_addr, local_mem_tpc_bytes=bytes_per_tpc) \
                                         .signal(self.timeline_signal, self.next_timeline()).submit(self)

  def invalidate_caches(self):
    rmctrl.fb_flush_gpu_cache(self.fd_ctl, self.root, self.subdevice,
      flags=((nv_gpu.NV2080_CTRL_FB_FLUSH_GPU_CACHE_FLAGS_WRITE_BACK_YES << 2) | (nv_gpu.NV2080_CTRL_FB_FLUSH_GPU_CACHE_FLAGS_INVALIDATE_YES << 3) |
             (nv_gpu.NV2080_CTRL_FB_FLUSH_GPU_CACHE_FLAGS_FLUSH_MODE_FULL_CACHE << 4)))

  def on_device_hang(self):
    # Prepare fault report.
    # TODO: Restore the GPU using NV83DE_CTRL_CMD_CLEAR_ALL_SM_ERROR_STATES if needed.

    report = []
    sm_errors = rmctrl.debug_read_all_sm_error_states(self.fd_ctl, self.root, self.debugger, hTargetChannel=self.debug_channel, numSMsToRead=100)

    if sm_errors.mmuFault.valid:
      mmu_info = rmctrl.debug_read_mmu_fault_info(self.fd_ctl, self.root, self.debugger)
      for i in range(mmu_info.count):
        pfinfo = mmu_info.mmuFaultInfoList[i]
        report += [f"MMU fault: 0x{pfinfo.faultAddress:X} | {NV_PFAULT_FAULT_TYPE[pfinfo.faultType]} | {NV_PFAULT_ACCESS_TYPE[pfinfo.accessType]}"]
        if DEBUG >= 5:
          report += ["GPU mappings:\n"+"\n".join(f"\t0x{x:X} - 0x{x+y-1:X} | {self._debug_mappings[(x,y)]}" for x,y in sorted(self._debug_mappings))]
    else:
      for i, e in enumerate(sm_errors.smErrorStateArray):
        if e.hwwGlobalEsr or e.hwwWarpEsr: report += [f"SM {i} fault: esr={e.hwwGlobalEsr} warp_esr={e.hwwWarpEsr} warp_pc={e.hwwWarpEsrPc64}"]

    raise RuntimeError("\n".join(report))
