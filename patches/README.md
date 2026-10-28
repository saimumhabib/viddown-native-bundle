# Vendored patches

These five patches are copied UNCHANGED from
<https://github.com/beeware/cpython-android-source-deps> at commit
`5521f2c16ee87dd7f9d081421351f675fac2ee45` (MIT licence, see
`LICENSE.beeware-cpython-android-source-deps`). They are the patches CPython's own
Android port uses to build OpenSSL and libffi for Android.

We take only the patch SOURCE. We do **not** use that project's prebuilt release
binaries (which CPython's `Android/android.py` downloads), because VidDown builds every
native component from source. OpenSSL and libffi are compiled by
`scripts/build/build_python.sh` from the pinned, hash-verified upstream tarballs.

| file | purpose |
|---|---|
| openssl/at_secure.patch | Android always sets AT_SECURE; avoid treating the app as setuid |
| openssl/configuration.patch | adds `android-python32/64` targets with a plain `.so` extension |
| openssl/soname-3.patch | names the libs `libcrypto_python.so` / `libssl_python.so` so they never clash with Android's own libcrypto/libssl |
| libffi/long_double.patch | long double handling on Android |
| libffi/open_temp_exec_file.patch | temp-file location for the closure trampolines on Android |

If a patch stops applying after a version bump, the build fails loudly (`patch` exits
non-zero). Re-vendor from the newer upstream commit and update the commit id above.
