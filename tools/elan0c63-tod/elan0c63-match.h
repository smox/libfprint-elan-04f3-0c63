/*
 * Descriptor matching for the ELAN 04f3:0c63 - C interface
 * Copyright (C) 2026 Michael Brunner
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#pragma once

#include <glib.h>
#include <stdint.h>

G_BEGIN_DECLS

/* Raw sensor geometry. The device reports these, but the driver hard-codes
 * the expected values so a mismatch is caught rather than silently accepted. */
#define ELAN0C63_WIDTH  80
#define ELAN0C63_HEIGHT 80

/* Rows 0 and 1 read out of range on every capture, rows 2 and 3 partially.
 * A three pixel border removes all 231 anomalies measured across a corpus of
 * 123 captures and keeps 5476 of 6400 pixels. */
#define ELAN0C63_BORDER 3

/* Score threshold, derived from measurement rather than chosen: across 6615
 * impostor comparisons the highest score observed was 4. */
#define ELAN0C63_MATCH_THRESHOLD 5

/* Enrolment stages. Recognition at zero false accepts rises monotonically
 * with gallery size: 53 % at 3, 67 % at 5, 83 % at 10, 88.5 % at 14. The
 * curve had not saturated, so this sits at the upper end of what is still
 * tolerable to enrol by hand. */
#define ELAN0C63_ENROLL_STAGES 12

/* Opaque handle holding the keypoints and descriptors of one capture. */
typedef struct _Elan0c63Features Elan0c63Features;

/**
 * elan0c63_features_extract:
 * @frames: @frame_count raw frames, each ELAN0C63_WIDTH * ELAN0C63_HEIGHT
 *   uint16 samples in row order
 * @frame_count: number of frames captured for this press
 * @background: the empty-sensor reference of the same session
 *
 * Picks the sharpest of the supplied frames and extracts its features.
 *
 * The sharpest frame is the one yielding the most keypoints. Averaging the
 * frames was measured to be worse: the single best frame beat the mean in 120
 * of 123 captures by a median factor of 1.26, because the skin deforms between
 * frames. Aligning the frames first did not help either, which is what marks
 * the deformation as elastic rather than a rigid shift.
 *
 * Returns: (transfer full) (nullable): the features, or %NULL on failure
 */
Elan0c63Features *elan0c63_features_extract (const uint16_t *frames,
                                             guint           frame_count,
                                             const uint16_t *background);

/**
 * elan0c63_features_keypoint_count:
 *
 * Returns: how many keypoints the capture yielded
 */
guint elan0c63_features_keypoint_count (Elan0c63Features *features);

/**
 * elan0c63_features_serialise:
 *
 * Returns: (transfer full): a self-describing byte blob for storage
 */
GBytes *elan0c63_features_serialise (Elan0c63Features *features);

/**
 * elan0c63_features_deserialise:
 *
 * Returns: (transfer full) (nullable): the features, or %NULL if the blob is
 *   malformed or of an unknown version
 */
Elan0c63Features *elan0c63_features_deserialise (GBytes *blob);

/**
 * elan0c63_features_compare:
 *
 * Matches @probe against @gallery: nearest-neighbour descriptor matching, a
 * Lowe ratio test, then a RANSAC similarity fit over the surviving pairs.
 *
 * Returns: the number of geometrically consistent correspondences
 */
guint elan0c63_features_compare (Elan0c63Features *probe,
                                 Elan0c63Features *gallery);

void elan0c63_features_free (Elan0c63Features *features);

G_DEFINE_AUTOPTR_CLEANUP_FUNC (Elan0c63Features, elan0c63_features_free)

G_END_DECLS
