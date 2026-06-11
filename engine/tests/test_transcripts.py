import unittest
from pathlib import Path

from transcript_replay import replay_with_python, transcript_paths


class PythonTranscriptReplayTest(unittest.TestCase):
    def test_replays_all_transcripts(self):
        repo = Path(__file__).resolve().parents[2]
        transcripts = list(transcript_paths(repo))

        self.assertTrue(transcripts, "expected at least one transcript")
        for transcript in transcripts:
            with self.subTest(transcript=transcript.name):
                replay_with_python(repo, transcript)


if __name__ == "__main__":
    unittest.main()
