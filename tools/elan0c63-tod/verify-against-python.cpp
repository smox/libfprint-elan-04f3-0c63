/* Prueft die C++-Implementierung gegen die Python-Referenz.
 * Liest exportierte Rohaufnahmen, gibt Keypointzahlen und die vollstaendige
 * Score-Matrix aus. Kein Bestandteil des Treibers. */
#include "elan0c63-match.h"
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <algorithm>
#include <cstring>
#include <string>
#include <glib.h>

int main (int argc, char **argv)
{
  if (argc < 2) { fprintf (stderr, "Aufruf: %s VERZEICHNIS\n", argv[0]); return 64; }

  GDir *dir = g_dir_open (argv[1], 0, nullptr);
  if (!dir) { fprintf (stderr, "Verzeichnis nicht lesbar\n"); return 1; }

  std::vector<std::string> names;
  const char *name;
  while ((name = g_dir_read_name (dir)))
    if (g_str_has_suffix (name, ".bin")) names.push_back (name);
  g_dir_close (dir);
  std::sort (names.begin (), names.end ());

  std::vector<Elan0c63Features *> feats;
  const size_t px = ELAN0C63_WIDTH * ELAN0C63_HEIGHT;

  for (const auto &n : names) {
      char *path = g_build_filename (argv[1], n.c_str (), nullptr);
      gchar *data = nullptr; gsize len = 0;
      if (!g_file_get_contents (path, &data, &len, nullptr)) { g_free (path); continue; }
      g_free (path);
      /* Layout: u32 frame_count | background (px u16) | frames (count * px u16) */
      uint32_t count; memcpy (&count, data, 4);
      const uint16_t *bg = reinterpret_cast<const uint16_t *> (data + 4);
      const uint16_t *fr = bg + px;
      feats.push_back (elan0c63_features_extract (fr, count, bg));
      g_free (data);
  }

  printf ("KEYPOINTS\n");
  for (size_t i = 0; i < names.size (); i++)
    printf ("%s\t%u\n", names[i].c_str (), elan0c63_features_keypoint_count (feats[i]));

  printf ("SCORES\n");
  for (size_t i = 0; i < feats.size (); i++)
    for (size_t j = i + 1; j < feats.size (); j++)
      printf ("%s\t%s\t%u\n", names[i].c_str (), names[j].c_str (),
              elan0c63_features_compare (feats[i], feats[j]));

  for (auto *f : feats) elan0c63_features_free (f);
  return 0;
}
