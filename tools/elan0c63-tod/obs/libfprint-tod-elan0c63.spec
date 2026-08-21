#
# Spec for the ELAN 04f3:0c63 driver with descriptor-based matching.
#
# The package installs a single TOD module. It replaces no system library and
# changes no existing configuration.
#

Name:           libfprint-tod-elan0c63
Version:        0.1.0
Release:        1%{?dist}
Summary:        libfprint driver for ELAN 04f3:0c63 with descriptor matching
Summary(de):    libfprint-Treiber für ELAN 04f3:0c63 mit Deskriptorvergleich

License:        LGPL-2.1-or-later
URL:            https://github.com/smox/libfprint-elan-04f3-0c63
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  pkgconfig
BuildRequires:  pkgconfig(libfprint-2-tod-1)
BuildRequires:  pkgconfig(libfprint-2)
BuildRequires:  opencv-devel
BuildRequires:  glib2-devel
# The driver calls g_usb_device_* directly. gusb is only a Requires.private of
# libfprint-2-tod-1, so it must be asked for explicitly.
BuildRequires:  pkgconfig(gusb)

Requires:       libfprint-2-tod1
Requires:       fprintd

# libfprint dlopen()s the module, so rpm picks up the library dependencies
# correctly from the ELF entries.

# Do not derive this with pkg-config: %global is expanded while parsing, before
# BuildRequires are installed. The path is part of the TOD ABI and is verified
# against pkg-config in %check, where the tool does exist.
%global tod_driversdir %{_libdir}/libfprint-2/tod-1

%description
A libfprint driver for the ELAN 04f3:0c63 fingerprint sensor found in several
TUXEDO and Clevo laptops.

The in-tree elan driver treats this device as a swipe sensor and matches with
NBIS/Bozorth. That cannot work here: the sensor is 80x80 px at a measured
498 dpi, so 16.6 mm², while NBIS needs about 37 mm² before it finds the ten
minutiae Bozorth requires. Across 123 measured captures not one exceeded four.

This driver keeps the native press capture and matches local SIFT descriptors
with a RANSAC geometry check instead. It is loaded at runtime as a TOD module
and replaces no system file. It takes precedence over the in-tree driver for
this one USB id only; every other device keeps its usual driver.

Measured on the developer's hardware: 9 of 10 genuine attempts recognised with
no false accept in 10 attempts with other fingers, against 1 of 5 with the
in-tree driver. That sample is small - see the README for what the numbers do
and do not support.

%description -l de
Ein libfprint-Treiber für den Fingerabdrucksensor ELAN 04f3:0c63, der in
mehreren TUXEDO- und Clevo-Geräten verbaut ist.

Der eingebaute elan-Treiber behandelt das Gerät als Wischsensor und vergleicht
über NBIS/Bozorth. Das kann hier nicht funktionieren: Der Sensor misst 80x80
Pixel bei gemessenen 498 dpi, also 16,6 mm²; NBIS benötigt rund 37 mm², bevor
es die von Bozorth geforderten zehn Minutien findet. Über 123 vermessene
Aufnahmen erreichte keine einzige mehr als vier.

Dieser Treiber behält die native Auflage-Aufnahme und vergleicht stattdessen
lokale SIFT-Deskriptoren mit einer RANSAC-Geometrieprüfung. Er wird zur
Laufzeit als TOD-Modul geladen und ersetzt keine Systemdatei. Vorrang hat er
ausschließlich für diese eine USB-Kennung.

%prep
%autosetup

%build
TOD_CFLAGS=$(pkg-config --cflags libfprint-2-tod-1)
CV_CFLAGS=$(pkg-config --cflags-only-I opencv4)

g++ %{optflags} -std=c++17 -fPIC -c elan0c63-match.cpp -o match.o \
    ${CV_CFLAGS} $(pkg-config --cflags glib-2.0)

gcc %{optflags} -std=gnu11 -fPIC -c elan0c63-tod.c -o tod.o \
    -I. ${TOD_CFLAGS}

# Only the OpenCV modules actually used; the opencv4 pkg-config module would
# otherwise drag in dnn, videoio and videostab.
# --no-undefined turns a symbol that only resolves transitively at runtime into
# a build failure here, where it is cheap to find.
g++ -shared -Wl,-z,relro,-z,now -Wl,--no-undefined \
    -o libfprint-tod-elan0c63.so tod.o match.o \
    $(pkg-config --libs libfprint-2-tod-1 libfprint-2 gusb) \
    -lopencv_core -lopencv_imgproc -lopencv_features2d \
    -lopencv_calib3d -lopencv_flann

%install
install -D -m 0755 libfprint-tod-elan0c63.so \
    %{buildroot}%{tod_driversdir}/libfprint-tod-elan0c63.so

%check
# Without the entry point libfprint will not load the module, which would
# otherwise only surface on the user's machine.
nm -D --defined-only libfprint-tod-elan0c63.so | grep -q fpi_tod_shared_driver_get_type

# Cross-check that the hard-coded path matches what libfprint actually expects.
# pkg-config is available at this point.
test "$(pkg-config --variable=tod_driversdir libfprint-2-tod-1)" = "%{tod_driversdir}"

%post
# fprintd is D-Bus activated; stopping it is enough for the next access to pick
# up the new driver.
systemctl stop fprintd.service >/dev/null 2>&1 || :

%postun
if [ $1 -eq 0 ]; then
    systemctl stop fprintd.service >/dev/null 2>&1 || :
fi

%files
%license COPYING
%doc README.md
%dir %{_libdir}/libfprint-2
%dir %{tod_driversdir}
%{tod_driversdir}/libfprint-tod-elan0c63.so

%changelog
* Thu Aug 20 2026 Michael Brunner <michael.brunner@sm0x.org> - 0.1.0-1
- Initial package. Descriptor-based matching for ELAN 04f3:0c63.
- Native press capture instead of the synthetic swipe assembly.
- Calibration retries instead of a hard failure when the sensor is not clear.
