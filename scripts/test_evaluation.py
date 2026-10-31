"""Checks the evaluator's matching/counting, not the transcription model."""
import unittest

from scripts.evaluate_transcription import metrics


class EvaluationMetricsTest(unittest.TestCase):
    def test_extra_prediction_lowers_precision_even_when_reference_is_found(self):
        reference = [{"pitch": 60, "start": 1, "end": 2}]
        predicted = [
            {"pitch": 60, "start": 1.02, "end": 2.1},
            {"pitch": 72, "start": 1.02, "end": 2.1},
        ]
        result = metrics(reference, predicted, 0.05)
        self.assertEqual(result["matched_exact_pitch_and_onset"], 1)
        self.assertEqual(result["false_positive_notes"], 1)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 1)
        self.assertAlmostEqual(result["onset_mae_seconds"], 0.02)
        self.assertAlmostEqual(result["offset_mae_seconds"], 0.1)

    def test_duplicate_reference_cannot_reuse_one_detection(self):
        reference = [{"pitch": 60, "start": 1, "end": 2}] * 2
        result = metrics(reference, [{"pitch": 60, "start": 1.01, "end": 2}], 0.05)
        self.assertEqual(result["matched_exact_pitch_and_onset"], 1)
        self.assertEqual(result["missed_notes"], 1)

    def test_pitch_and_timing_mismatches_are_not_true_positives(self):
        reference = [{"pitch": 60, "start": 1, "end": 2}]
        late = metrics(reference, [{"pitch": 60, "start": 1.06, "end": 2}], 0.05)
        octave = metrics(reference, [{"pitch": 72, "start": 1.02, "end": 2}], 0.05)
        self.assertEqual(late["f1"], 0)
        self.assertEqual(octave["f1"], 0)
        self.assertEqual(octave["pitch_mae_semitones_within_150ms"], 12)
        self.assertEqual(octave["octave_errors_within_150ms"], 1)

    def test_silence_uses_false_positive_counts_not_invented_recall(self):
        quiet = metrics([], [], 0.05)
        noisy = metrics([], [{"pitch": 60, "start": 1, "end": 2}], 0.05)
        self.assertEqual(quiet["false_positive_notes"], 0)
        self.assertIsNone(quiet["f1"])
        self.assertEqual(noisy["false_positive_notes"], 1)
        self.assertIsNone(noisy["recall"])


if __name__ == "__main__":
    unittest.main()
