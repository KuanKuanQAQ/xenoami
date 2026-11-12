# 测试方法

## initramfs

- busybox：`/usr/bin/busybox` 是静态链接的，直接用。
- libevl 测试程序（`build/libevl/tests/`）是动态链接的，依赖：`libevl.so.9`、`libbpf.so.1`、`libc.so.6`、`libelf.so.1`、`libz.so.1`、`ld-linux-x86-64.so.2`，按原路径复制进去（或设 `LD_LIBRARY_PATH`）。也可以用 `meson configure -Ddefault_library=static` 重编。
- init 脚本挂载 `proc`、`sysfs`、`devtmpfs`（`/dev/evl/*` 依赖 devtmpfs）。

```
initramfs/
  bin/busybox (+ busybox --install -s)
  lib/x86_64-linux-gnu/{libc.so.6,libelf.so.1,libz.so.1}
  lib64/ld-linux-x86-64.so.2
  usr/lib/{libevl.so.9,libbpf.so.1}
  usr/bin/evl-tests/*
  init
```

打包：`find . | cpio -o -H newc | gzip > ../initramfs.cpio.gz`

## 启动

账号在 kvm 组，可用 KVM（延迟数字比纯模拟准得多）：

```bash
qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 2G \
  -kernel ~/xenoami/build/linux-evl-qemu/arch/x86/boot/bzImage \
  -initrd ~/xenoami/build/initramfs.cpio.gz \
  -append "console=ttyS0" -nographic
```

调试加 `-s -S`，`gdb vmlinux` 后 `target remote :1234`。

**改过的内核只在虚拟机里跑，不要在宿主机上试。**

## A 类 helper 的触发方式

| 函数 | 怎么验证 |
|---|---|
| `strcmp`（init.c）| 启动日志出现 EVL "core started" |
| `rb_*`（memory.c）、`find_first_bit` | 启动后任何 EVL 操作；`clock-timer-periodic`、`evl test` 全套 |
| `rb_*`（poll.c）| `poll-many`、`poll-multiple`、`poll-nested` |
| `rb_*`（observable.c）| `observable-*` 测试 |
| `find_first_zero_bit`、`strcmp`、`hashlen_string`（factory.c）| 创建 element；重复创建同名 public element 应返回 `-EEXIST` |
| `strncpy`（thread.c）| 创建任何 EVL 线程 |
| `ns_to_timespec64` | `clock_gettime` 相关测试 |
| `strsep`/`simple_strtol` | 写 `/sys/devices/virtual/evl/clock/monotonic/gravity`（路径待确认，格式见 `gravity_store`，`clock.c:966`）|
| `strncmp` | 往 control 的 `state` 写 `start`/`stop`（写 `stop` 会停掉 EVL）|
| `int_sqrt` | 跑 `latmus` |

## 确认调用的是 LKL 版本

- 静态：`objdump -d vmlinux --disassemble=__evl_schedule | grep call`，应看到 `call ... <lkl_evl_find_first_bit>`。
- 动态：gdb 对 `lkl_evl_rb_insert_color` 下断点，跑测试看是否命中。

## 延迟

实时性验证需要和基线对比：迁移前后各跑一次 `latmus`。今天只验证功能；换成 EVL 原语实现 host_ops 时再系统地测。
