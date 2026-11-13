# debhelper

A build tool, not something we ship to machines: stonking packages `iproute2`,
`ethtool` and `libnvme` at debhelper compat level 14, which needs debhelper 14
(noble has 13.14, resolute 13.31).

## noble

`noble/patches/0001-dh_shlibdeps-no-package-option-for-old-dpkg.patch`:
debhelper 14 calls `dpkg-shlibdeps --package=<pkg>` at compat 14, and noble's
dpkg-dev 1.22.6 rejects the option. The patch falls back to the compat-13 call.
