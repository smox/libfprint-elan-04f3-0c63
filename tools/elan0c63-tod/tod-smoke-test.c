/*
 * Loads a TOD module the way libfprint does and checks that the driver it
 * exports is actually usable.
 *
 * nm proves the symbols exist. It does not prove that the GType registers,
 * that the class is an FpDevice, or that the USB id table survived the build.
 * Those failures look like a clean install followed by "no device found" on
 * the user's machine, which is the hardest kind to diagnose remotely.
 *
 * Deliberately independent of the distribution: it only needs libfprint,
 * glib and dlopen.
 *
 *   cc -o tod-smoke-test tod-smoke-test.c \
 *      $(pkg-config --cflags --libs libfprint-2-tod-1 libfprint-2) -ldl
 *   ./tod-smoke-test /usr/lib64/libfprint-2/tod-1/libfprint-tod-elan0c63.so
 */

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#include <fprint.h>
#include <glib-object.h>

/* FpDeviceClass and FpIdEntry are part of the private driver API, so this test
 * needs the TOD headers rather than just the public fprint.h. */
#include <fpi-device.h>

/* The loader contract: libfprint resolves exactly this symbol.
 * See tod-shared-loader.c in libfprint. */
typedef GType (*tod_entry_fn) (void);

int
main (int argc, char **argv)
{
  if (argc != 2)
    {
      fprintf (stderr, "usage: %s <module.so>\n", argv[0]);
      return 64;
    }

  const char *path = argv[1];

  /* RTLD_NOW, not RTLD_LAZY: unresolved symbols must fail here rather than at
   * the first verification attempt months later. */
  void *handle = dlopen (path, RTLD_NOW | RTLD_LOCAL);
  if (handle == NULL)
    {
      fprintf (stderr, "FAIL dlopen: %s\n", dlerror ());
      return 1;
    }
  printf ("  dlopen(RTLD_NOW):  ok\n");

  dlerror ();
  tod_entry_fn entry = (tod_entry_fn) dlsym (handle, "fpi_tod_shared_driver_get_type");
  const char *err = dlerror ();
  if (entry == NULL || err != NULL)
    {
      fprintf (stderr, "FAIL dlsym: %s\n", err ? err : "symbol is NULL");
      return 1;
    }
  printf ("  entry point:       resolved\n");

  GType type = entry ();
  if (type == G_TYPE_INVALID || type == 0)
    {
      fprintf (stderr, "FAIL: entry point returned no GType\n");
      return 1;
    }
  printf ("  GType:             %s\n", g_type_name (type));

  if (!g_type_is_a (type, FP_TYPE_DEVICE))
    {
      fprintf (stderr, "FAIL: %s is not an FpDevice\n", g_type_name (type));
      return 1;
    }
  printf ("  is an FpDevice:    yes\n");

  /* Instantiating the class runs the driver's class_init, which is where the
   * id table, the scan type and the number of enrol stages are set. A driver
   * that registers but declares no device would pass every check above. */
  FpDeviceClass *klass = g_type_class_ref (type);
  if (klass == NULL)
    {
      fprintf (stderr, "FAIL: class_init did not produce a class\n");
      return 1;
    }

  printf ("  driver id:         %s\n", klass->id ? klass->id : "(none)");
  printf ("  full name:         %s\n", klass->full_name ? klass->full_name : "(none)");
  printf ("  device type:       %s\n",
          klass->type == FP_DEVICE_TYPE_USB ? "USB" :
          klass->type == FP_DEVICE_TYPE_UDEV ? "udev" : "virtual");
  printf ("  scan type:         %s\n",
          klass->scan_type == FP_SCAN_TYPE_PRESS ? "press" : "swipe");
  printf ("  enrol stages:      %d\n", klass->nr_enroll_stages);

  if (klass->id == NULL || klass->full_name == NULL)
    {
      fprintf (stderr, "FAIL: driver declares no id or name\n");
      return 1;
    }

  if (klass->id_table == NULL || klass->id_table[0].vid == 0)
    {
      fprintf (stderr, "FAIL: driver declares no USB ids, it will never bind\n");
      return 1;
    }

  printf ("  declared devices:\n");
  for (const FpIdEntry *e = klass->id_table; e->vid != 0 || e->pid != 0; e++)
    printf ("    %04x:%04x\n", e->vid, e->pid);

  if (klass->nr_enroll_stages < 1)
    {
      fprintf (stderr, "FAIL: enrol stages must be at least 1\n");
      return 1;
    }

  g_type_class_unref (klass);

  /* Not dlclose()d on purpose: GType registrations cannot be unregistered,
   * and unmapping the code behind a live GType is a use-after-free. libfprint
   * keeps TOD modules loaded for the same reason. */

  printf ("\n  smoke test passed\n");
  return 0;
}
