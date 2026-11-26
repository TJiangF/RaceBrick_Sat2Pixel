import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="racebrick-v2-test-")
os.environ["RACEBRICK_DB"] = os.path.join(_TMP, "racebrick.db")

import racebrick_platform as platform


class V2SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        platform._init_db()

    def test_track_revision_and_session_laps(self):
        track_uuid = "11111111-1111-4111-8111-111111111111"
        revision_id = "22222222-2222-4222-8222-222222222222"
        track = platform.TrackPayload(
            local_id=3,
            payload={
                "id": 3,
                "name": "Test Track",
                "geo": 0,
                "length_m": 100.0,
                "width": 5.0,
                "start_width": 5.0,
                "start_f": 0.1,
                "reverse": 0,
                "start_offset": 0.0,
                "sections": [0.5],
                "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                "uuid": track_uuid,
                "version": 4,
                "revision_id": revision_id,
            },
        )
        with platform.db() as conn:
            changed, _ = platform._upsert_track(conn, "esp32s3-test", track)
            self.assertTrue(changed)
            changed, _ = platform._upsert_track(conn, "esp32s3-test", track)
            self.assertFalse(changed)
            row = conn.execute("SELECT * FROM tracks WHERE track_uuid=?", (track_uuid,)).fetchone()
            self.assertEqual(row["track_version"], 4)
            self.assertEqual(row["revision_id"], revision_id)
            revision = conn.execute(
                "SELECT * FROM track_revisions WHERE track_uuid=? AND revision_id=?",
                (track_uuid, revision_id),
            ).fetchone()
            self.assertIsNotNone(revision)

            session_uuid = "33333333-3333-4333-8333-333333333333"
            csv_text = "Format,3\nSession title,\"Test Track\"\n"
            for i, (elapsed, lap) in enumerate(
                [(0.1, 1), (0.2, 1), (0.3, 1), (0.4, 1), (0.5, 1),
                 (0.6, 1), (0.7, 1), (0.8, 2), (0.9, 2), (1.0, 2)]
            ):
                csv_text += f"{1795000000 + i},{i},{lap},{elapsed},31.0,121.0,20.0\n"
            session = platform.SessionPayload(
                local_slot=0,
                payload={
                    "slot": 0,
                    "id": 1795000000,
                    "track": 3,
                    "track_name": "Test Track",
                    "start": 1795000000,
                    "end": 1795000010,
                    "duration_ms": 10000,
                    "samples": 10,
                    "sample_size": 20,
                    "laps": 2,
                    "deleted_laps": 0,
                    "lap_times": [2.0, 3.0],
                    "session_uuid": session_uuid,
                    "track_uuid": track_uuid,
                    "track_revision_id": revision_id,
                    "track_version": 4,
                },
                csv=csv_text,
            )
            changed, _ = platform._upsert_session(conn, "esp32s3-test", session)
            self.assertTrue(changed)
            srow = conn.execute(
                "SELECT * FROM sessions WHERE session_uuid=?", (session_uuid,)
            ).fetchone()
            self.assertEqual(srow["track_uuid"], track_uuid)
            self.assertEqual(srow["track_revision_id"], revision_id)
            self.assertEqual(srow["sample_size"], 20)
            self.assertEqual(srow["best_lap"], 1)
            laps = conn.execute(
                "SELECT * FROM lap_records WHERE session_uuid=? ORDER BY lap_number",
                (session_uuid,),
            ).fetchall()
            self.assertEqual([r["lap_number"] for r in laps], [1, 2])
            self.assertEqual([r["lap_time_ms"] for r in laps], [2000, 3000])
            self.assertEqual(sum(r["best_lap"] for r in laps), 1)


if __name__ == "__main__":
    unittest.main()
