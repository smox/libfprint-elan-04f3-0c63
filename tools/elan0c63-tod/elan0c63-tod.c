/*
 * ELAN 04f3:0c63 driver with descriptor-based matching
 * Copyright (C) 2026 Michael Brunner
 *
 * The USB protocol is taken from libfprint's in-tree elan driver
 * (libfprint/drivers/elan.c), Copyright (C) 2017 Igor Filatov, LGPL-2.1+.
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * Why a separate driver instead of a patch to elan.c:
 *
 * FpImageDevice hands every capture to NBIS and compares it with Bozorth;
 * fpi-image-device.c calls fpi_print_bz3_match() directly with no hook for an
 * alternative. On this sensor that path cannot work. It is 80x80 px at a
 * measured 498 dpi, so 16.6 mm², while NBIS needs about 37 mm² before it finds
 * the ten minutiae Bozorth requires. Across 123 captures from two people not
 * one exceeded four minutiae.
 *
 * The in-tree driver works around the area limit by asking the user to swipe
 * and assembling the frames into a taller image. That is what makes minutiae
 * possible at all, and it is also what makes them unstable: the assembly drops
 * 30 of 80 rows per frame, cannot represent a stationary finger, and leaves a
 * third of its output empty.
 *
 * This driver takes the other route the Windows driver for the same device
 * takes: keep the native press capture and use local descriptors, which have
 * no minimum area.
 */

#define FP_COMPONENT "elan0c63"

#include <drivers_api.h>

#include "elan0c63-match.h"

/* Protocol constants, from elan.h */
#define ELAN_VEND_ID 0x04f3
#define ELAN_PROD_ID 0x0c63

#define EP_CMD_OUT (0x1 | FPI_USB_ENDPOINT_OUT)
#define EP_CMD_IN  (0x3 | FPI_USB_ENDPOINT_IN)
#define EP_IMG_IN  (0x2 | FPI_USB_ENDPOINT_IN)

#define CMD_TIMEOUT_MS  10000
#define FINGER_TIMEOUT_MS 200

#define CALIBRATION_MAX_DELTA 500

/* Two independent limits. The in-tree driver conflates them: it allows ten
 * attempts, decrements before testing, and so effectively polls nine times or
 * about 0.45 s. Upstream merge request 217 raises that number generally.
 *
 * CALIBRATION_POLLS bounds the status polling inside one recalibration,
 * CALIBRATION_ROUNDS how often a recalibration may be repeated. Sharing one
 * counter between both, as an earlier version of this driver did, exhausts the
 * budget in the first round. */
#define CALIBRATION_ROUNDS 5
#define CALIBRATION_POLLS  50

/* The device needs time between status reads. Without this delay the poll loop
 * completes in milliseconds and gives up long before the sensor is ready -
 * which is exactly how the first version of this driver failed. */
#define CALIBRATION_POLL_DELAY_MS 50

/* A background frame whose mean is far below the calibration mean means the
 * read did not return image data at all. Proceeding would compute a nonsense
 * delta and trigger an endless recalibration. */
#define BACKGROUND_MIN_PLAUSIBLE 100

#define FINGER_PRESENT   0x55
#define NOT_CALIBRATED   0xff

/* Frames captured per press. The gain from more saturates early: taking the
 * best of two rather than one gained 11 %, everything beyond that under half a
 * percent. Four leaves headroom without making the user wait. */
#define FRAMES_PER_PRESS 4

/* A capture yielding very few keypoints carries no usable identity. Rejecting
 * it as a retry is the same contract the NBIS path gets from the Bozorth
 * minimum: better to ask again than to store something unmatchable. The corpus
 * minimum over 123 captures was 39, so this only fires on genuinely broken
 * captures. */
#define MIN_USABLE_KEYPOINTS 20

struct _FpiDeviceElan0c63
{
  FpDevice          parent;

  FpiSsm           *task_ssm;
  FpiSsm           *cmd_ssm;
  FpiUsbTransfer   *cmd_transfer;

  guint8           *last_read;
  gsize             last_read_len;

  /* The timeout applies to the response, not to sending the command. It is
   * recorded when the command goes out and used when reading the reply. */
  guint             pending_timeout_ms;

  guint8            frame_width;
  guint8            frame_height;
  guint16           fw_version;

  guint16          *background;
  guint16          *frames;
  guint             frames_captured;

  /* Enrolment accumulates one feature set per stage. */
  GPtrArray        *stage_blobs;
  guint             stage;

  /* Deserialised gallery of the print being verified against. */
  GPtrArray        *gallery;
  gint              best_score;
  gboolean          matched;
};

G_DECLARE_FINAL_TYPE (FpiDeviceElan0c63, fpi_device_elan0c63, FPI,
                      DEVICE_ELAN0C63, FpDevice)
G_DEFINE_TYPE (FpiDeviceElan0c63, fpi_device_elan0c63, FP_TYPE_DEVICE)

static const FpIdEntry id_table[] = {
  { .vid = ELAN_VEND_ID, .pid = ELAN_PROD_ID },
  { .vid = 0, .pid = 0 },
};

/* ---------------------------------------------------------------- commands */

typedef struct
{
  guint8 bytes[2];
  gint   response_len;   /* -1 means a full raw frame */
  guint8 endpoint;
} ElanCmd;

static const ElanCmd cmd_get_sensor_dim = { { 0x00, 0x0c }, 4, EP_CMD_IN };
static const ElanCmd cmd_get_fw_ver     = { { 0x40, 0x19 }, 2, EP_CMD_IN };
static const ElanCmd cmd_get_image      = { { 0x00, 0x09 }, -1, EP_IMG_IN };
static const ElanCmd cmd_get_calib_status = { { 0x40, 0x23 }, 1, EP_CMD_IN };
static const ElanCmd cmd_get_calib_mean = { { 0x40, 0x24 }, 2, EP_CMD_IN };
static const ElanCmd cmd_led_on         = { { 0x40, 0x31 }, 0, EP_CMD_IN };
static const ElanCmd cmd_pre_scan       = { { 0x40, 0x3f }, 1, EP_CMD_IN };
static const ElanCmd cmd_stop           = { { 0x00, 0x0b }, 0, EP_CMD_IN };

static void
cmd_read_cb (FpiUsbTransfer *transfer, FpDevice *device,
             gpointer user_data, GError *error)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  if (error)
    {
      /* While waiting for further frames a timeout is the normal way to
       * notice the finger was lifted, not an error. Report an empty reply,
       * which the capture state reads as "no finger". */
      if (g_error_matches (error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT) &&
          self->pending_timeout_ms != 0)
        {
          g_clear_error (&error);
          g_clear_pointer (&self->last_read, g_free);
          self->last_read_len = 0;
          fpi_ssm_next_state (transfer->ssm);
          return;
        }

      fpi_ssm_mark_failed (transfer->ssm, error);
      return;
    }

  g_clear_pointer (&self->last_read, g_free);
  self->last_read_len = transfer->actual_length;
  self->last_read = g_memdup2 (transfer->buffer, transfer->actual_length);

  fpi_ssm_next_state (transfer->ssm);
}

static void
cmd_write_cb (FpiUsbTransfer *transfer, FpDevice *device,
              gpointer user_data, GError *error)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  const ElanCmd *cmd = user_data;
  gssize length;

  if (error)
    {
      fpi_ssm_mark_failed (transfer->ssm, error);
      return;
    }

  if (cmd->response_len == 0)
    {
      fpi_ssm_next_state (transfer->ssm);
      return;
    }

  length = cmd->response_len;
  if (length < 0)
    length = (gssize) self->frame_width * self->frame_height * 2;

  {
    g_autoptr(FpiUsbTransfer) read = fpi_usb_transfer_new (device);
    read->ssm = transfer->ssm;
    read->short_is_error = TRUE;
    fpi_usb_transfer_fill_bulk (read, cmd->endpoint, length);
    /* For gusb, 0 means wait indefinitely - exactly what is needed while the
     * sensor waits for a finger. */
    fpi_usb_transfer_submit (g_steal_pointer (&read),
                             self->pending_timeout_ms,
                             fpi_device_get_cancellable (device),
                             cmd_read_cb, NULL);
  }
}

static void
run_cmd (FpiSsm *ssm, FpDevice *device, const ElanCmd *cmd, guint timeout_ms)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  g_autoptr(FpiUsbTransfer) transfer = fpi_usb_transfer_new (device);

  /* Das Absenden der zwei Kommandobytes dauert immer gleich lang; nur die
   * Antwort kann beliebig lange auf sich warten lassen. */
  self->pending_timeout_ms = timeout_ms;

  transfer->ssm = ssm;
  transfer->short_is_error = TRUE;
  fpi_usb_transfer_fill_bulk_full (transfer, EP_CMD_OUT,
                                   (guint8 *) cmd->bytes, 2, NULL);
  fpi_usb_transfer_submit (g_steal_pointer (&transfer), CMD_TIMEOUT_MS,
                           fpi_device_get_cancellable (device),
                           cmd_write_cb, (gpointer) cmd);
}

/* ------------------------------------------------------------ frame layout */

/* The sensor sends the frame column-major ("the frame is vertical" in
 * elan.c). Unlike the in-tree driver we keep all rows: it caps frame_height at
 * ELAN_MAX_FRAME_HEIGHT (50) and discards 15 rows top and bottom, which throws
 * away 37.5 % of an already tiny sensor. That cap exists because tall frames
 * make the swipe assembly unreliable, and we do not assemble. */
static void
store_frame (FpiDeviceElan0c63 *self, guint16 *destination)
{
  const guint8 w = self->frame_width;
  const guint8 h = self->frame_height;
  const guint16 *raw = (const guint16 *) self->last_read;

  for (guint y = 0; y < h; y++)
    for (guint x = 0; x < w; x++)
      destination[y * w + x] = raw[x * h + y];
}

/* ------------------------------------------------------------- calibration */

enum calibrate_states {
  CALIB_GET_BACKGROUND,
  CALIB_SAVE_BACKGROUND,
  CALIB_GET_MEAN,
  CALIB_CHECK_NEEDED,
  CALIB_GET_STATUS,
  CALIB_CHECK_STATUS,
  CALIB_NUM_STATES,
};

typedef struct
{
  guint  rounds_left;    /* vollstaendige Rekalibrierungen */
  guint  polls_left;     /* Statusabfragen innerhalb einer Runde */
  guint8 status_seen;
} CalibrateData;

static void
calibrate_run_state (FpiSsm *ssm, FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  CalibrateData *data = fpi_ssm_get_data (ssm);
  const gsize pixels = (gsize) self->frame_width * self->frame_height;

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case CALIB_GET_BACKGROUND:
      run_cmd (ssm, device, &cmd_get_image, CMD_TIMEOUT_MS);
      break;

    case CALIB_SAVE_BACKGROUND:
      if (self->background == NULL)
        self->background = g_new0 (guint16, pixels);
      store_frame (self, self->background);
      fpi_ssm_next_state (ssm);
      break;

    case CALIB_GET_MEAN:
      run_cmd (ssm, device, &cmd_get_calib_mean, CMD_TIMEOUT_MS);
      break;

    case CALIB_CHECK_NEEDED:
      {
        /* Big-endian 16 bit. The in-tree driver multiplies the high byte by
         * 0xff, which is not an injective decoding: 255 byte pairs collide. */
        const guint16 calib_mean =
          ((guint16) self->last_read[0] << 8) | self->last_read[1];
        guint64 sum = 0;
        guint32 background_mean, delta;

        for (gsize i = 0; i < pixels; i++)
          sum += self->background[i];
        background_mean = (guint32) (sum / pixels);

        delta = background_mean > calib_mean ? background_mean - calib_mean
                                             : calib_mean - background_mean;

        fp_dbg ("calibration mean %u, background mean %u, delta %u",
                calib_mean, background_mean, delta);

        if (background_mean < BACKGROUND_MIN_PLAUSIBLE)
          {
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                           "Background read returned no image data (mean %u)",
                                                           background_mean));
            break;
          }

        if (delta <= CALIBRATION_MAX_DELTA)
          {
            fpi_ssm_mark_completed (ssm);
            break;
          }

        if (data->rounds_left == 0)
          {
            /* A background far above the calibration mean means something is
             * touching the sensor. Asking the user to lift their finger and
             * try again is more useful than aborting the whole enrolment. */
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_retry_new_msg (FP_DEVICE_RETRY_REMOVE_FINGER,
                                                           "Sensor is not clear (background %u against %u); lift your finger and try again",
                                                           background_mean, calib_mean));
            break;
          }

        data->rounds_left--;
        data->polls_left = CALIBRATION_POLLS;
        data->status_seen = 0;
        fpi_ssm_next_state (ssm);
        break;
      }

    case CALIB_GET_STATUS:
      run_cmd (ssm, device, &cmd_get_calib_status, CMD_TIMEOUT_MS);
      break;

    case CALIB_CHECK_STATUS:
      /* 0x01 means busy, 0x03 means ready. Shortly after the request the
       * device can still answer 0x03 from the previous cycle, so wait for a
       * 0x01 first to be sure a full cycle completed. */
      if (data->status_seen == 0x01 && self->last_read[0] == 0x03)
        {
          fpi_ssm_jump_to_state (ssm, CALIB_GET_BACKGROUND);
          break;
        }

      if (self->last_read[0] == 0x01)
        data->status_seen = 0x01;

      if (data->polls_left == 0)
        {
          fpi_ssm_mark_failed (ssm,
                               fpi_device_retry_new_msg (FP_DEVICE_RETRY_REMOVE_FINGER,
                                                         "Sensor did not finish calibrating; lift your finger and try again"));
          break;
        }

      data->polls_left--;
      /* Without this delay the poll budget is spent within milliseconds,
       * long before the sensor is ready. */
      fpi_ssm_jump_to_state_delayed (ssm, CALIB_GET_STATUS,
                                     CALIBRATION_POLL_DELAY_MS);
      break;

    default:
      g_assert_not_reached ();
    }
}

static FpiSsm *
calibrate_ssm_new (FpDevice *device)
{
  FpiSsm *ssm = fpi_ssm_new (device, calibrate_run_state, CALIB_NUM_STATES);
  CalibrateData *data = g_new0 (CalibrateData, 1);

  data->rounds_left = CALIBRATION_ROUNDS;
  data->polls_left = CALIBRATION_POLLS;
  fpi_ssm_set_data (ssm, data, g_free);

  return ssm;
}

/* ---------------------------------------------------------------- capture */

enum capture_states {
  CAPTURE_LED_ON,
  CAPTURE_WAIT_FINGER,
  CAPTURE_READ_FRAME,
  CAPTURE_STORE_FRAME,
  CAPTURE_STOP,
  CAPTURE_NUM_STATES,
};

static void
capture_run_state (FpiSsm *ssm, FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  const gsize pixels = (gsize) self->frame_width * self->frame_height;

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case CAPTURE_LED_ON:
      self->frames_captured = 0;
      run_cmd (ssm, device, &cmd_led_on, CMD_TIMEOUT_MS);
      break;

    case CAPTURE_WAIT_FINGER:
      /* No timeout on the first frame: the user gets as long as they need.
       * Subsequent frames use a short one so lifting the finger ends the
       * capture instead of blocking. */
      run_cmd (ssm, device, &cmd_pre_scan,
               self->frames_captured == 0 ? 0 : FINGER_TIMEOUT_MS);
      break;

    case CAPTURE_READ_FRAME:
      if (self->last_read == NULL || self->last_read_len < 1)
        {
          /* An empty reply means the timeout expired. After the first frame
           * that is the normal way to notice the finger was lifted; before it
           * there is nothing to work with and the capture has to be retried. */
          if (self->frames_captured > 0)
            fpi_ssm_jump_to_state (ssm, CAPTURE_STOP);
          else
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_retry_new_msg (FP_DEVICE_RETRY_GENERAL,
                                                           "No finger detected"));
          break;
        }

      if (self->last_read[0] == NOT_CALIBRATED)
        {
          fpi_ssm_mark_failed (ssm,
                               fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                         "Sensor reports it is not calibrated"));
          break;
        }

      if (self->last_read[0] != FINGER_PRESENT)
        {
          /* Finger lifted. Whatever we have is what we work with. */
          fpi_ssm_jump_to_state (ssm, CAPTURE_STOP);
          break;
        }

      if (self->frames_captured == 0)
        fpi_device_report_finger_status (device, FP_FINGER_STATUS_PRESENT);

      run_cmd (ssm, device, &cmd_get_image, CMD_TIMEOUT_MS);
      break;

    case CAPTURE_STORE_FRAME:
      store_frame (self, self->frames + self->frames_captured * pixels);
      self->frames_captured++;

      if (self->frames_captured >= FRAMES_PER_PRESS)
        fpi_ssm_next_state (ssm);
      else
        fpi_ssm_jump_to_state (ssm, CAPTURE_WAIT_FINGER);
      break;

    case CAPTURE_STOP:
      /* Without this reset the sensor stays in the previous capture's wait
       * state, and the next command sequence reads the reply to an earlier
       * request. The in-tree driver sends a stop after every capture for the
       * same reason (elan_stop_capture). */
      run_cmd (ssm, device, &cmd_stop, CMD_TIMEOUT_MS);
      break;

    default:
      g_assert_not_reached ();
    }
}

/* --------------------------------------------------------------- matching */

static GBytes *
extract_blob (FpiDeviceElan0c63 *self, GError **error)
{
  g_autoptr(Elan0c63Features) features = NULL;
  guint keypoints;

  features = elan0c63_features_extract (self->frames, self->frames_captured,
                                        self->background);

  if (features == NULL)
    {
      g_propagate_error (error,
                         fpi_device_retry_new_msg (FP_DEVICE_RETRY_GENERAL,
                                                   "No usable features in this capture"));
      return NULL;
    }

  keypoints = elan0c63_features_keypoint_count (features);
  fp_dbg ("capture yielded %u keypoints from %u frames",
          keypoints, self->frames_captured);

  if (keypoints < MIN_USABLE_KEYPOINTS)
    {
      g_propagate_error (error,
                         fpi_device_retry_new_msg (FP_DEVICE_RETRY_GENERAL,
                                                   "Only %u features found, please scan again",
                                                   keypoints));
      return NULL;
    }

  return elan0c63_features_serialise (features);
}

static gint
score_against_print (FpPrint *print, GBytes *probe_blob)
{
  g_autoptr(Elan0c63Features) probe = NULL;
  g_autoptr(GVariant) data = NULL;
  g_autoptr(GVariant) entries = NULL;
  GVariantIter iter;
  GVariant *entry;
  gint best = 0;

  g_object_get (print, "fpi-data", &data, NULL);
  if (data == NULL || !g_variant_check_format_string (data, "(qaay)", FALSE))
    return -1;

  probe = elan0c63_features_deserialise (probe_blob);
  if (probe == NULL)
    return -1;

  {
    guint16 version;
    g_variant_get (data, "(q@aay)", &version, &entries);
    if (version != 1)
      return -1;
  }

  /* The score is the best over the gallery, not the sum. Each entry sees a
   * different part of the finger; a probe only has to line up with one of
   * them. Measured: recognition at zero false accepts rises from 53 % with
   * three entries to 88.5 % with fourteen. */
  g_variant_iter_init (&iter, entries);
  while ((entry = g_variant_iter_next_value (&iter)))
    {
      g_autoptr(GVariant) held = entry;
      g_autoptr(GBytes) blob = NULL;
      g_autoptr(Elan0c63Features) gallery = NULL;
      gsize len = 0;
      const void *raw = g_variant_get_fixed_array (held, &len, 1);
      gint score;

      blob = g_bytes_new (raw, len);
      gallery = elan0c63_features_deserialise (blob);
      if (gallery == NULL)
        continue;

      score = (gint) elan0c63_features_compare (probe, gallery);
      if (score > best)
        best = score;
    }

  return best;
}

/* ------------------------------------------------------------------ enroll */

enum enroll_states {
  ENROLL_CALIBRATE,
  ENROLL_CAPTURE,
  ENROLL_PROCESS,
  ENROLL_NUM_STATES,
};

static void
enroll_run_state (FpiSsm *ssm, FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case ENROLL_CALIBRATE:
      fpi_ssm_start_subsm (ssm, calibrate_ssm_new (device));
      break;

    case ENROLL_CAPTURE:
      fpi_ssm_start_subsm (ssm,
                           fpi_ssm_new (device, capture_run_state,
                                        CAPTURE_NUM_STATES));
      break;

    case ENROLL_PROCESS:
      {
        g_autoptr(GError) error = NULL;
        GBytes *blob;

        fpi_device_report_finger_status (device, FP_FINGER_STATUS_NONE);

        blob = extract_blob (self, &error);
        if (blob == NULL)
          {
            /* A retryable error keeps the stage counter where it is, so the
             * user is simply asked for the same stage again. */
            fpi_device_enroll_progress (device, self->stage, NULL,
                                        g_steal_pointer (&error));
            fpi_ssm_jump_to_state (ssm, ENROLL_CAPTURE);
            break;
          }

        g_ptr_array_add (self->stage_blobs, blob);
        self->stage++;
        fpi_device_enroll_progress (device, self->stage, NULL, NULL);

        if (self->stage >= ELAN0C63_ENROLL_STAGES)
          fpi_ssm_mark_completed (ssm);
        else
          fpi_ssm_jump_to_state (ssm, ENROLL_CAPTURE);
        break;
      }

    default:
      g_assert_not_reached ();
    }
}

static void
enroll_complete (FpiSsm *ssm, FpDevice *device, GError *error)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  FpPrint *print = NULL;
  GVariantBuilder builder;

  self->task_ssm = NULL;

  if (error)
    {
      fpi_device_enroll_complete (device, NULL, error);
      return;
    }

  fpi_device_get_enroll_data (device, &print);

  g_variant_builder_init (&builder, G_VARIANT_TYPE ("aay"));
  for (guint i = 0; i < self->stage_blobs->len; i++)
    {
      GBytes *blob = g_ptr_array_index (self->stage_blobs, i);
      gsize len = 0;
      const guint8 *raw = g_bytes_get_data (blob, &len);

      g_variant_builder_add_value (&builder,
                                   g_variant_new_fixed_array (G_VARIANT_TYPE_BYTE,
                                                              raw, len, 1));
    }

  fpi_print_set_type (print, FPI_PRINT_RAW);
  fpi_print_set_device_stored (print, FALSE);
  g_object_set (print, "fpi-data",
                g_variant_new ("(qaay)", (guint16) 1, &builder), NULL);

  fpi_device_enroll_complete (device, g_object_ref (print), NULL);
}

static void
dev_enroll (FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  self->stage = 0;
  g_ptr_array_set_size (self->stage_blobs, 0);

  self->task_ssm = fpi_ssm_new (device, enroll_run_state, ENROLL_NUM_STATES);
  fpi_ssm_start (self->task_ssm, enroll_complete);
}

/* ------------------------------------------------------- verify / identify */

enum verify_states {
  VERIFY_CALIBRATE,
  VERIFY_CAPTURE,
  VERIFY_PROCESS,
  VERIFY_NUM_STATES,
};

static void
verify_run_state (FpiSsm *ssm, FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case VERIFY_CALIBRATE:
      fpi_ssm_start_subsm (ssm, calibrate_ssm_new (device));
      break;

    case VERIFY_CAPTURE:
      fpi_ssm_start_subsm (ssm,
                           fpi_ssm_new (device, capture_run_state,
                                        CAPTURE_NUM_STATES));
      break;

    case VERIFY_PROCESS:
      {
        g_autoptr(GError) error = NULL;
        g_autoptr(GBytes) blob = NULL;

        fpi_device_report_finger_status (device, FP_FINGER_STATUS_NONE);

        blob = extract_blob (self, &error);
        if (blob == NULL)
          {
            fpi_ssm_mark_failed (ssm, g_steal_pointer (&error));
            break;
          }

        if (fpi_device_get_current_action (device) == FPI_DEVICE_ACTION_VERIFY)
          {
            FpPrint *enrolled = NULL;
            gint score;

            fpi_device_get_verify_data (device, &enrolled);
            score = score_against_print (enrolled, blob);

            if (score < 0)
              {
                fpi_ssm_mark_failed (ssm,
                                     fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID));
                break;
              }

            fp_dbg ("verify best score %d, threshold %d",
                    score, ELAN0C63_MATCH_THRESHOLD);

            self->best_score = score;
            self->matched = score >= ELAN0C63_MATCH_THRESHOLD;
            fpi_device_verify_report (device,
                                      self->matched ? FPI_MATCH_SUCCESS
                                                    : FPI_MATCH_FAIL,
                                      NULL, NULL);
          }
        else
          {
            GPtrArray *prints = NULL;
            FpPrint *winner = NULL;
            gint best = 0;

            fpi_device_get_identify_data (device, &prints);

            for (guint i = 0; prints != NULL && i < prints->len; i++)
              {
                FpPrint *candidate = g_ptr_array_index (prints, i);
                gint score = score_against_print (candidate, blob);

                if (score > best)
                  {
                    best = score;
                    winner = candidate;
                  }
              }

            fp_dbg ("identify best score %d over %u prints", best,
                    prints != NULL ? prints->len : 0);

            if (best >= ELAN0C63_MATCH_THRESHOLD)
              fpi_device_identify_report (device, winner, NULL, NULL);
            else
              fpi_device_identify_report (device, NULL, NULL, NULL);
          }

        fpi_ssm_mark_completed (ssm);
        break;
      }

    default:
      g_assert_not_reached ();
    }
}

static void
verify_complete (FpiSsm *ssm, FpDevice *device, GError *error)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  self->task_ssm = NULL;

  if (fpi_device_get_current_action (device) == FPI_DEVICE_ACTION_VERIFY)
    fpi_device_verify_complete (device, error);
  else
    fpi_device_identify_complete (device, error);
}

static void
dev_verify (FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  self->task_ssm = fpi_ssm_new (device, verify_run_state, VERIFY_NUM_STATES);
  fpi_ssm_start (self->task_ssm, verify_complete);
}

/* -------------------------------------------------------- open / close */

enum activate_states {
  ACTIVATE_GET_FW,
  ACTIVATE_STORE_FW,
  ACTIVATE_GET_DIM,
  ACTIVATE_STORE_DIM,
  ACTIVATE_NUM_STATES,
};

static void
activate_run_state (FpiSsm *ssm, FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case ACTIVATE_GET_FW:
      run_cmd (ssm, device, &cmd_get_fw_ver, CMD_TIMEOUT_MS);
      break;

    case ACTIVATE_STORE_FW:
      self->fw_version = ((guint16) self->last_read[0] << 8) | self->last_read[1];
      fp_dbg ("firmware 0x%04x", self->fw_version);
      fpi_ssm_next_state (ssm);
      break;

    case ACTIVATE_GET_DIM:
      run_cmd (ssm, device, &cmd_get_sensor_dim, CMD_TIMEOUT_MS);
      break;

    case ACTIVATE_STORE_DIM:
      /* This device is one of the rotated ones: width in byte 2, height in
       * byte 0. Some sensors report a zero based index instead of a count. */
      self->frame_width = self->last_read[2];
      self->frame_height = self->last_read[0];

      if (self->frame_width % 2 == 1 && self->frame_height % 2 == 1)
        {
          self->frame_width++;
          self->frame_height++;
        }

      fp_dbg ("sensor %ux%u", self->frame_width, self->frame_height);

      if (self->frame_width != ELAN0C63_WIDTH ||
          self->frame_height != ELAN0C63_HEIGHT)
        {
          fpi_ssm_mark_failed (ssm,
                               fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                                         "Expected %ux%u, device reports %ux%u",
                                                         ELAN0C63_WIDTH, ELAN0C63_HEIGHT,
                                                         self->frame_width,
                                                         self->frame_height));
          break;
        }

      self->frames = g_new0 (guint16,
                             (gsize) self->frame_width * self->frame_height *
                             FRAMES_PER_PRESS);
      fpi_ssm_mark_completed (ssm);
      break;

    default:
      g_assert_not_reached ();
    }
}

static void
open_complete (FpiSsm *ssm, FpDevice *device, GError *error)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  self->task_ssm = NULL;
  fpi_device_open_complete (device, error);
}

static void
dev_open (FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  GError *error = NULL;

  if (!g_usb_device_claim_interface (fpi_device_get_usb_device (device),
                                     0, 0, &error))
    {
      fpi_device_open_complete (device, error);
      return;
    }

  self->task_ssm = fpi_ssm_new (device, activate_run_state, ACTIVATE_NUM_STATES);
  fpi_ssm_start (self->task_ssm, open_complete);
}

static void
dev_close (FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);
  GError *error = NULL;

  g_clear_pointer (&self->background, g_free);
  g_clear_pointer (&self->frames, g_free);
  g_clear_pointer (&self->last_read, g_free);

  g_usb_device_release_interface (fpi_device_get_usb_device (device),
                                  0, 0, &error);
  fpi_device_close_complete (device, error);
}

static void
dev_cancel (FpDevice *device)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (device);

  if (self->task_ssm != NULL)
    {
      g_autoptr(FpiUsbTransfer) transfer = fpi_usb_transfer_new (device);
      transfer->short_is_error = FALSE;
      fpi_usb_transfer_fill_bulk_full (transfer, EP_CMD_OUT,
                                       (guint8 *) cmd_stop.bytes, 2, NULL);
      fpi_usb_transfer_submit (g_steal_pointer (&transfer), CMD_TIMEOUT_MS,
                               NULL, NULL, NULL);
    }
}

/* ------------------------------------------------------------------ class */

/* libfprint waehlt bei mehreren passenden Treibern den mit der hoechsten
 * Punktzahl (fp-context.c, usb_device_added_cb). Ohne usb_discover bekommt ein
 * Treiber 50; der eingebaute elan-Treiber beansprucht dieselbe USB-Kennung und
 * liegt damit bei 50.
 *
 * We report a higher score, but only for this exact model. Every other ELAN
 * device is left alone, and no FP_DRIVERS_ALLOWLIST is needed - that would
 * disable all other drivers globally and is unusable for a system install.
 */
static gint
dev_usb_discover (GUsbDevice *device)
{
  if (g_usb_device_get_vid (device) != ELAN_VEND_ID ||
      g_usb_device_get_pid (device) != ELAN_PROD_ID)
    return 0;

  return 90;
}

static void
fpi_device_elan0c63_init (FpiDeviceElan0c63 *self)
{
  self->stage_blobs = g_ptr_array_new_with_free_func ((GDestroyNotify) g_bytes_unref);
}

static void
fpi_device_elan0c63_finalize (GObject *object)
{
  FpiDeviceElan0c63 *self = FPI_DEVICE_ELAN0C63 (object);

  g_clear_pointer (&self->stage_blobs, g_ptr_array_unref);
  g_clear_pointer (&self->gallery, g_ptr_array_unref);
  g_clear_pointer (&self->background, g_free);
  g_clear_pointer (&self->frames, g_free);
  g_clear_pointer (&self->last_read, g_free);

  G_OBJECT_CLASS (fpi_device_elan0c63_parent_class)->finalize (object);
}

static void
fpi_device_elan0c63_class_init (FpiDeviceElan0c63Class *klass)
{
  GObjectClass *object_class = G_OBJECT_CLASS (klass);
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);

  object_class->finalize = fpi_device_elan0c63_finalize;

  dev_class->id = "elan0c63";
  dev_class->full_name = "ElanTech 04f3:0c63 (descriptor matching)";
  dev_class->type = FP_DEVICE_TYPE_USB;
  dev_class->id_table = id_table;

  /* Press, not swipe. The in-tree elan driver declares FP_SCAN_TYPE_SWIPE for
   * every Elan device without exception; this sensor is a square area sensor
   * and the vendor's own Windows driver waits for a press. */
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->nr_enroll_stages = ELAN0C63_ENROLL_STAGES;

  dev_class->usb_discover = dev_usb_discover;
  dev_class->open = dev_open;
  dev_class->close = dev_close;
  dev_class->enroll = dev_enroll;
  dev_class->verify = dev_verify;
  dev_class->identify = dev_verify;
  dev_class->cancel = dev_cancel;

  fpi_device_class_auto_initialize_features (dev_class);
}

/* The symbol libfprint's TOD loader looks for in every shared driver. */
GType
fpi_tod_shared_driver_get_type (void)
{
  return fpi_device_elan0c63_get_type ();
}
