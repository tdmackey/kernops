# makedumpfile

Ubuntu's newest package (stonking and tenacious, 1.7.9) declares Linux 7.0 as
the newest kernel it supports; upstream 1.8.0 raises that to 7.3. No Ubuntu
series packages 1.8.0 yet (checked 2026-11-13), so `patches/` carries the fixes
from the 1.7.9..1.8.0 range on top of Ubuntu's 1.7.9. Ubuntu already carries
the other 7.x fix from that range itself
(`fix-compound-tail-pages-linux-7.1.patch`).

| Patch | Upstream commit | Why |
|---|---|---|
| 0001 | cecce00ab4f3 | derive the `mem_section` map mask from `SIZE(page)` |
| 0002 | b7587f1e8f45 | arm64: 5-level page tables (4K pages, 52-bit VA) |
| 0003 | fe9858fb7a59 | crash at exit of a parallel (`--num-threads`) run |
| 0004-0009 | e63708a4865a, 93d65a2616b8, d96b21320ea0, 69585d6567cd, 80f6586b119a, 755160b42784 | bounds checks when reading a damaged dump |

Not taken: the new `--extension` mechanism (ten commits, a feature) and the
riscv64 fixes.

Drop this directory when a row in `userspace/matrix.tsv` can point at a
packaged 1.8.0 or later.
