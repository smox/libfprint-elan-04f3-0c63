/*
 * Descriptor matching for the ELAN 04f3:0c63
 * Copyright (C) 2026 Michael Brunner
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * Why this exists at all: the sensor is 80x80 px at a measured 498 dpi, so
 * 16.6 mm². NBIS needs roughly 37 mm² before it finds the ten minutiae
 * Bozorth requires. Across 123 captures of this device not one exceeded four
 * minutiae. A minutiae matcher therefore cannot work here regardless of image
 * quality, while SIFT finds a median of 142 keypoints on the same images.
 *
 * Every constant below was measured against that corpus. The values are not
 * defaults and not guesses; changing one without re-measuring will quietly
 * degrade the result.
 */

#include "elan0c63-match.h"

#include <opencv2/calib3d.hpp>
#include <opencv2/features2d.hpp>
#include <opencv2/imgproc.hpp>

#include <algorithm>
#include <cstring>
#include <vector>

namespace {

/* Contrast-limited adaptive histogram equalisation, as in the SIGFM draft.
 * Measured: 2.0 and 3.0 equivalent, 5.0 worse. */
constexpr double kClaheClip = 3.0;
constexpr int kClaheGrid = 8;

/* The cropped capture is 74x74. A little upscaling gives SIFT extra octaves
 * to work with. More is not better: 4x dropped recognition from 82.1 % to
 * 69.9 % because the interpolation invents structure. */
constexpr int kUpscale = 2;

/* OpenCV's default. An earlier version used 0.02, reasoned as "the image is
 * small and low contrast". That reasoning was wrong: a low threshold admits
 * keypoints made of noise, which carry no identity and match a stranger's
 * finger as readily as one's own. Measured 19.5 % against 69.9 %. */
constexpr double kSiftContrast = 0.04;
constexpr double kSiftEdge = 12.0;
constexpr double kSiftSigma = 1.2;
constexpr int kSiftOctaveLayers = 3;

/* Lowe ratio. The SIGFM draft uses 0.75; 0.70 measured better here (84.6 %
 * against 82.1 %). At 0.85 the separation collapses to 25.2 %. */
constexpr float kRatio = 0.70f;

/* A finger is placed shifted and rotated but not perspectively distorted, so
 * a similarity transform is the right model. The reprojection threshold is
 * flat between 3 and 10 px. */
constexpr double kRansacReproj = 3.0;
constexpr size_t kRansacMinMatches = 4;

constexpr uint32_t kBlobMagic = 0x30433633; /* "0C63" */
constexpr uint16_t kBlobVersion = 1;

constexpr int kCropped = ELAN0C63_WIDTH - 2 * ELAN0C63_BORDER;
constexpr size_t kFramePixels =
  static_cast<size_t> (ELAN0C63_WIDTH) * ELAN0C63_HEIGHT;

/* Linear interpolated percentile, matching numpy's default so the C++ and the
 * Python reference implementation agree on the same input. */
double
percentile (std::vector<float> &sorted, double q)
{
  if (sorted.empty ())
    return 0.0;
  const double pos = q / 100.0 * static_cast<double> (sorted.size () - 1);
  const size_t lo = static_cast<size_t> (pos);
  const size_t hi = std::min (lo + 1, sorted.size () - 1);
  return sorted[lo] + (pos - static_cast<double> (lo)) * (sorted[hi] - sorted[lo]);
}

/* Background subtraction, border crop, normalisation, upscale, CLAHE. */
cv::Mat
prepare (const uint16_t *frame, const uint16_t *background)
{
  cv::Mat signal (kCropped, kCropped, CV_32F);
  std::vector<float> values;
  values.reserve (static_cast<size_t> (kCropped) * kCropped);

  for (int y = 0; y < kCropped; y++)
    {
      for (int x = 0; x < kCropped; x++)
        {
          const size_t idx =
            static_cast<size_t> (y + ELAN0C63_BORDER) * ELAN0C63_WIDTH +
            (x + ELAN0C63_BORDER);
          /* Kept signed: the difference is genuinely negative in the valleys,
           * and clamping it away costs contrast. */
          const float v = static_cast<float> (frame[idx]) -
                          static_cast<float> (background[idx]);
          signal.at<float> (y, x) = v;
          values.push_back (v);
        }
    }

  std::sort (values.begin (), values.end ());
  const double lo = percentile (values, 1.0);
  const double hi = percentile (values, 99.0);
  const double span = std::max (hi - lo, 1e-9);

  cv::Mat scaled (kCropped, kCropped, CV_8U);
  for (int y = 0; y < kCropped; y++)
    for (int x = 0; x < kCropped; x++)
      {
        double t = (signal.at<float> (y, x) - lo) / span;
        t = std::clamp (t, 0.0, 1.0);
        scaled.at<uint8_t> (y, x) = static_cast<uint8_t> (t * 255.0);
      }

  cv::Mat upscaled;
  cv::resize (scaled, upscaled, cv::Size (), kUpscale, kUpscale, cv::INTER_CUBIC);

  cv::Mat equalised;
  cv::createCLAHE (kClaheClip, cv::Size (kClaheGrid, kClaheGrid))
    ->apply (upscaled, equalised);

  return equalised;
}

} /* namespace */

struct _Elan0c63Features
{
  std::vector<cv::KeyPoint> keypoints;
  cv::Mat descriptors;   /* CV_32F, one row per keypoint */
};

Elan0c63Features *
elan0c63_features_extract (const uint16_t *frames,
                           guint           frame_count,
                           const uint16_t *background)
{
  if (frames == nullptr || background == nullptr || frame_count == 0)
    return nullptr;

  auto sift = cv::SIFT::create (0, kSiftOctaveLayers, kSiftContrast,
                                kSiftEdge, kSiftSigma);

  std::vector<cv::KeyPoint> best_keypoints;
  cv::Mat best_descriptors;

  /* Pick the sharpest frame, measured as the one yielding the most keypoints.
   * That criterion measured marginally better than a Fourier ridge-clarity
   * estimate (85.4 % against 84.6 %) and needs no extra machinery. */
  for (guint i = 0; i < frame_count; i++)
    {
      const cv::Mat image = prepare (frames + i * kFramePixels, background);

      std::vector<cv::KeyPoint> keypoints;
      cv::Mat descriptors;
      sift->detectAndCompute (image, cv::noArray (), keypoints, descriptors);

      if (keypoints.size () > best_keypoints.size ())
        {
          best_keypoints = std::move (keypoints);
          best_descriptors = descriptors.clone ();
        }
    }

  if (best_keypoints.empty ())
    return nullptr;

  auto *features = new _Elan0c63Features;
  features->keypoints = std::move (best_keypoints);
  features->descriptors = std::move (best_descriptors);
  return features;
}

guint
elan0c63_features_keypoint_count (Elan0c63Features *features)
{
  return features ? static_cast<guint> (features->keypoints.size ()) : 0;
}

/* Blob layout, little endian:
 *   magic u32 | version u16 | count u16 | count * (x f32, y f32) | count * 128 u8
 *
 * Only the coordinates are kept from each keypoint; scale, angle and response
 * are not needed once the descriptor exists. Descriptors are stored as bytes:
 * OpenCV returns floats, but every value measured across the corpus was an
 * integer in 0..220, so this is lossless. Verified to give bit-identical
 * results at a quarter of the size, 17 KB per capture instead of 70 KB.
 */
GBytes *
elan0c63_features_serialise (Elan0c63Features *features)
{
  if (features == nullptr || features->descriptors.cols != 128)
    return nullptr;

  const uint16_t count = static_cast<uint16_t> (
    std::min<size_t> (features->keypoints.size (), UINT16_MAX));

  const size_t size = 4 + 2 + 2 +
                      static_cast<size_t> (count) * 2 * sizeof (float) +
                      static_cast<size_t> (count) * 128;

  auto *buffer = static_cast<uint8_t *> (g_malloc (size));
  size_t offset = 0;

  const uint32_t magic = kBlobMagic;
  memcpy (buffer + offset, &magic, 4); offset += 4;
  const uint16_t version = kBlobVersion;
  memcpy (buffer + offset, &version, 2); offset += 2;
  memcpy (buffer + offset, &count, 2); offset += 2;

  for (uint16_t i = 0; i < count; i++)
    {
      const float xy[2] = { features->keypoints[i].pt.x,
                            features->keypoints[i].pt.y };
      memcpy (buffer + offset, xy, sizeof (xy));
      offset += sizeof (xy);
    }

  for (uint16_t i = 0; i < count; i++)
    {
      const float *row = features->descriptors.ptr<float> (i);
      for (int d = 0; d < 128; d++)
        buffer[offset + d] = static_cast<uint8_t> (
          std::clamp (static_cast<int> (row[d] + 0.5f), 0, 255));
      offset += 128;
    }

  return g_bytes_new_take (buffer, size);
}

Elan0c63Features *
elan0c63_features_deserialise (GBytes *blob)
{
  gsize size = 0;
  const auto *buffer = static_cast<const uint8_t *> (g_bytes_get_data (blob, &size));

  if (buffer == nullptr || size < 8)
    return nullptr;

  uint32_t magic;
  uint16_t version, count;
  memcpy (&magic, buffer, 4);
  memcpy (&version, buffer + 4, 2);
  memcpy (&count, buffer + 6, 2);

  if (magic != kBlobMagic || version != kBlobVersion)
    return nullptr;

  const size_t expected = 8 + static_cast<size_t> (count) * 2 * sizeof (float) +
                          static_cast<size_t> (count) * 128;
  if (size != expected)
    return nullptr;

  auto *features = new _Elan0c63Features;
  features->keypoints.reserve (count);
  features->descriptors = cv::Mat (count, 128, CV_32F);

  size_t offset = 8;
  for (uint16_t i = 0; i < count; i++)
    {
      float xy[2];
      memcpy (xy, buffer + offset, sizeof (xy));
      offset += sizeof (xy);
      features->keypoints.emplace_back (cv::Point2f (xy[0], xy[1]), 1.0f);
    }

  for (uint16_t i = 0; i < count; i++)
    {
      float *row = features->descriptors.ptr<float> (i);
      for (int d = 0; d < 128; d++)
        row[d] = static_cast<float> (buffer[offset + d]);
      offset += 128;
    }

  return features;
}

guint
elan0c63_features_compare (Elan0c63Features *probe,
                           Elan0c63Features *gallery)
{
  if (probe == nullptr || gallery == nullptr)
    return 0;
  if (probe->descriptors.rows < 2 || gallery->descriptors.rows < 2)
    return 0;

  std::vector<std::vector<cv::DMatch> > candidates;
  cv::BFMatcher (cv::NORM_L2).knnMatch (probe->descriptors,
                                        gallery->descriptors,
                                        candidates, 2);

  /* Ridge patterns are highly self-similar, so most nearest neighbours are
   * ambiguous. The ratio test discards those; what survives still has to pass
   * the geometric check below. */
  std::vector<cv::Point2f> source, target;
  for (const auto &pair : candidates)
    {
      if (pair.size () < 2)
        continue;
      if (pair[0].distance >= kRatio * pair[1].distance)
        continue;
      source.push_back (probe->keypoints[pair[0].queryIdx].pt);
      target.push_back (gallery->keypoints[pair[0].trainIdx].pt);
    }

  if (source.size () < kRansacMinMatches)
    return 0;

  /* Without this stage any coincidentally similar texture counts. It is the
   * step the SIGFM draft lacks and that the Windows engine exposes through its
   * RANSAC_diff and AcceptRANSACCnt configuration. */
  cv::Mat inliers;
  cv::estimateAffinePartial2D (source, target, inliers, cv::RANSAC,
                               kRansacReproj, 5000, 0.99);

  if (inliers.empty ())
    return 0;

  return static_cast<guint> (cv::countNonZero (inliers));
}

void
elan0c63_features_free (Elan0c63Features *features)
{
  delete features;
}
