/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Tiny launcher: a PIE executable that runs the embedded CPython interpreter.
 *
 * It is packaged as libviddown_python.so (an ELF executable with a lib*.so name) so
 * Android installs it into the app's nativeLibraryDir, the one place an app may
 * execute binaries from on targetSdk >= 29. The app starts it with ProcessBuilder:
 *
 *   PYTHONHOME=<files>/python  PYTHONPATH=<site-packages>  LD_LIBRARY_PATH=<nativeLibraryDir>
 *   <nativeLibraryDir>/libviddown_python.so -m yt_dlp ...
 *
 * Running yt-dlp in its own process (not inside the app process) lets us kill the whole
 * process tree on cancel, isolates crashes, and loads Python only when the yt-dlp engine
 * is selected.
 */
#include <Python.h>

int main(int argc, char **argv) {
    return Py_BytesMain(argc, argv);
}
