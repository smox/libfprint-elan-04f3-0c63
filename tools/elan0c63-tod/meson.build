project('libfprint-tod-elan0c63', ['c', 'cpp'],
  version: '0.1.0',
  license: 'LGPL-2.1-or-later',
  default_options: [
    'c_std=gnu11',
    'cpp_std=c++17',
    'warning_level=2',
    'optimization=2',
  ],
)

# The private driver API. Also pulls in glib, gusb and the include paths.
tod_dep = dependency('libfprint-2-tod-1')

# The public types - fp_device_get_type() and friends live here, not in the
# TOD library, which only carries the 235 fpi_* symbols.
fprint_dep = dependency('libfprint-2')

# The driver calls g_usb_device_claim_interface() and friends directly. gusb is
# only a Requires.private of libfprint-2-tod-1, so it supplies include paths but
# not link flags. Without this the module links anyway on a toolchain that
# tolerates undefined symbols, and resolves them at runtime through libfprint's
# own gusb - which works by luck, not by contract. Debian's meson defaults pass
# -Wl,--no-undefined and turn that luck into a build failure.
gusb_dep = dependency('gusb')

# Only the OpenCV modules actually used. Linking the opencv4 pkg-config module
# as a whole drags in dnn, videoio and videostab, none of which this driver
# touches.
opencv_deps = [
  dependency('opencv4', modules: ['opencv_core']),
]

cxx = meson.get_compiler('cpp')
opencv_libs = []
foreach name : ['opencv_core', 'opencv_imgproc', 'opencv_features2d', 'opencv_calib3d', 'opencv_flann']
  opencv_libs += cxx.find_library(name, required: true)
endforeach

opencv_inc = include_directories()
opencv_cflags = run_command('pkg-config', '--cflags-only-I', 'opencv4',
                            check: true).stdout().strip().split()

driver = shared_library('fprint-tod-elan0c63',
  sources: [
    'elan0c63-tod.c',
    'elan0c63-match.cpp',
  ],
  dependencies: [tod_dep, fprint_dep, gusb_dep] + opencv_libs,
  cpp_args: opencv_cflags,
  # A TOD module is dlopen()ed, never linked against, so no versioned soname.
  name_prefix: 'lib',
  install: true,
  install_dir: tod_dep.get_variable(pkgconfig: 'tod_driversdir'),
)

# Offline check that the module still reproduces the reference numbers.
# Not installed; built only when the corpus is available.
if get_option('build_verifier')
  executable('elan0c63-verify',
    sources: ['verify-against-python.cpp', 'elan0c63-match.cpp'],
    dependencies: [dependency('glib-2.0')] + opencv_libs,
    cpp_args: opencv_cflags,
    install: false,
  )
endif

summary({
  'driver directory': tod_dep.get_variable(pkgconfig: 'tod_driversdir'),
}, section: 'Install')
