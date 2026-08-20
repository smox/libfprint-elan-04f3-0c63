#
# Spec für den ELAN-04f3:0c63-Treiber mit deskriptorbasiertem Vergleich.
#
# Das Paket installiert ein einzelnes TOD-Modul. Es ersetzt keine
# Systembibliothek und verändert keine bestehende Konfiguration.
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

Requires:       libfprint-2-tod1
Requires:       fprintd

# Das Modul wird von libfprint per dlopen geladen; RPM sieht die
# Bibliotheksabhängigkeiten daher korrekt über die ELF-Einträge.

# Nicht per pkg-config ermitteln: %global wird beim Parsen ausgewertet, also
# bevor die BuildRequires installiert sind. Der Pfad ist Teil der TOD-ABI und
# wird im %check gegen pkg-config geprueft, wo das Werkzeug dann existiert.
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

# Nur die tatsächlich benutzten OpenCV-Module. Das pkg-config-Paket opencv4
# zöge sonst dnn, videoio und videostab mit herein.
g++ -shared -Wl,-z,relro,-z,now -o libfprint-tod-elan0c63.so tod.o match.o \
    $(pkg-config --libs libfprint-2-tod-1 libfprint-2) \
    -lopencv_core -lopencv_imgproc -lopencv_features2d \
    -lopencv_calib3d -lopencv_flann

%install
install -D -m 0755 libfprint-tod-elan0c63.so \
    %{buildroot}%{tod_driversdir}/libfprint-tod-elan0c63.so

%check
# Ohne den Einstiegspunkt lädt libfprint das Modul nicht - das fällt sonst erst
# beim Benutzer auf.
nm -D --defined-only libfprint-tod-elan0c63.so | grep -q fpi_tod_shared_driver_get_type

# Gegenprobe, dass der fest eingetragene Pfad dem entspricht, was libfprint
# tatsaechlich erwartet. Hier ist pkg-config verfuegbar.
test "$(pkg-config --variable=tod_driversdir libfprint-2-tod-1)" = "%{tod_driversdir}"

%post
# fprintd wird über D-Bus aktiviert; ein Stopp genügt, damit der nächste
# Zugriff den neuen Treiber lädt.
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
