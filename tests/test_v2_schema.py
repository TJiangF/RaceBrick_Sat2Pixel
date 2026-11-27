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
            manifest = platform._cloud_manifest(conn, "esp32s3-test")
            self.assertEqual(manifest["protocol"], 2)
            self.assertEqual(manifest["tracks"][0]["track_id"], track_uuid)
            self.assertEqual(manifest["tracks"][0]["version"], 4)
            self.assertEqual(manifest["sessions"][0]["session_id"], session_uuid)
            self.assertEqual(manifest["sessions"][0]["track_revision_id"], revision_id)


    def test_sharing_defaults_private_and_owner_scoped(self):
        def make_user(name):
            with platform.db() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                    (name, platform._hash_password("password123"), "user", platform._now()),
                )
                return cur.lastrowid

        owner_id = make_user("share-owner")
        other_id = make_user("share-other")
        admin_id = make_user("share-admin")
        owner = {"id": owner_id, "username": "share-owner", "role": "user"}
        other = {"id": other_id, "username": "share-other", "role": "user"}
        admin = {"id": admin_id, "username": "share-admin", "role": "admin"}
        uid = "esp32s3-share-test"
        track_uuid = "66666666-6666-4666-8666-666666666666"
        session_uuid = "77777777-7777-4777-8777-777777777777"
        with platform.db() as conn:
            conn.execute(
                "INSERT INTO devices(uid,name,owner_id,created_at) VALUES(?,?,?,?)",
                (uid, "share-test", owner_id, platform._now()),
            )
            conn.execute(
                """INSERT INTO tracks(device_uid,local_id,name,payload,content_hash,
                   revision,deleted,updated_at,track_uuid,track_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (uid, 1, "Share Track", '{}', "track-hash", 1, 0,
                 platform._now(), track_uuid, 1),
            )
            conn.execute(
                """INSERT INTO sessions(device_uid,local_slot,payload,csv,content_hash,
                   revision,deleted,updated_at,session_uuid,lap_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (uid, 1, '{}', "", "session-hash", 1, 0,
                 platform._now(), session_uuid, 0),
            )

        with platform.db() as conn:
            view = platform.device_sharing(uid, owner)
            self.assertEqual(view["tracks"][0]["is_public"], False)
            self.assertIsNone(view["tracks"][0]["public_id"])
            self.assertEqual(view["sessions"][0]["laps_public"], False)
            self.assertEqual(view["sessions"][0]["raw_public"], False)

        with self.assertRaises(platform.HTTPException) as ctx:
            platform.device_sharing(uid, other)
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(platform.HTTPException) as ctx:
            platform.device_sharing(uid, admin)
        self.assertEqual(ctx.exception.status_code, 403)

        with platform.db() as conn:
            first = platform.set_track_sharing(
                uid, track_uuid, platform.TrackShareBody(public=True), owner)
            second = platform.set_track_sharing(
                uid, track_uuid, platform.TrackShareBody(public=True), owner)
            self.assertTrue(first["is_public"])
            self.assertTrue(first["public_id"].startswith("rbt_v1_"))
            self.assertEqual(first["public_id"], second["public_id"])

            laps = platform.set_session_sharing(
                uid, session_uuid, platform.SessionShareBody(laps_public=True), owner)
            self.assertTrue(laps["laps_public"])
            self.assertFalse(laps["raw_public"])
            raw = platform.set_session_sharing(
                uid, session_uuid, platform.SessionShareBody(raw_public=True), owner)
            self.assertTrue(raw["laps_public"])
            self.assertTrue(raw["raw_public"])
            self.assertTrue(raw["public_id"].startswith("rbs_v1_"))

            off = platform.set_track_sharing(
                uid, track_uuid, platform.TrackShareBody(public=False), owner)
            self.assertFalse(off["is_public"])
            self.assertEqual(first["public_id"], off["public_id"])
            session_off = platform.set_session_sharing(
                uid, session_uuid,
                platform.SessionShareBody(laps_public=False, raw_public=False), owner)
            self.assertFalse(session_off["laps_public"])
            self.assertFalse(session_off["raw_public"])
            self.assertEqual(raw["public_id"], session_off["public_id"])

        with self.assertRaises(platform.HTTPException) as ctx:
            platform.set_track_sharing(
                uid, track_uuid, platform.TrackShareBody(public=True), other)
        self.assertEqual(ctx.exception.status_code, 403)


    def test_public_catalog_visibility_download_and_pagination(self):
        with platform.db() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                ("public-owner", platform._hash_password("password123"), "user", platform._now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO devices(uid,name,owner_id,created_at) VALUES(?,?,?,?)",
                ("esp32s3-public-test", "public-test", owner, platform._now()),
            )
            track_ids = []
            for i in range(3):
                track_uuid = f"8888888{i}-8888-4888-8888-88888888888{i}"
                payload = {
                    "uuid": track_uuid, "name": f"Public {i}", "geo": False,
                    "length_m": 300 + i, "width": 5, "start_width": 10,
                    "start_f": 0.1, "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
                }
                conn.execute(
                    """INSERT INTO tracks(device_uid,local_id,name,payload,content_hash,
                       revision,deleted,updated_at,track_uuid,track_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("esp32s3-public-test", i, f"Public {i}",
                     platform._canonical(payload), f"hash-{i}", 1, 0,
                     platform._now() + i, track_uuid, 1),
                )
                track_ids.append(track_uuid)

        self.assertEqual(platform.public_tracks()["tracks"], [])

        owner_user = {"id": owner, "username": "public-owner", "role": "user"}
        with platform.db() as conn:
            public_ids = [
                platform.set_track_sharing(
                    "esp32s3-public-test", uuid,
                    platform.TrackShareBody(public=True), owner_user,
                )["public_id"]
                for uuid in track_ids
            ]
        self.assertTrue(all(pid.startswith("rbt_v1_") for pid in public_ids))

        first = platform.public_tracks(limit=2)
        self.assertEqual(len(first["tracks"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        self.assertNotIn("device_uid", first["tracks"][0])
        self.assertNotIn("local_id", first["tracks"][0])
        second = platform.public_tracks(limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["tracks"]), 1)

        detail = platform.public_track(public_ids[0])
        self.assertEqual(detail["format"], platform.PUBLIC_TRACK_FORMAT)
        self.assertEqual(detail["track"]["public_id"], public_ids[0])
        self.assertEqual(detail["track"]["track_uuid"], track_ids[0])
        self.assertNotIn("device_uid", detail["track"])
        self.assertNotIn("local_id", detail["track"])
        self.assertGreaterEqual(len(detail["track"]["points"]), 3)

        with platform.db() as conn:
            platform.set_track_sharing(
                "esp32s3-public-test", track_ids[0],
                platform.TrackShareBody(public=False), owner_user,
            )
        with self.assertRaises(platform.HTTPException) as ctx:
            platform.public_track(public_ids[0])
        self.assertEqual(ctx.exception.status_code, 404)
        after = platform.public_tracks(limit=10)
        self.assertNotIn(public_ids[0], [t["public_id"] for t in after["tracks"]])


    def test_public_leaderboard_uses_shared_laps_and_stable_rank(self):
        with platform.db() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,public_name,created_at) VALUES(?,?,?,?,?)",
                ("leader-owner", platform._hash_password("password123"), "user", "Alice", platform._now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO devices(uid,name,owner_id,created_at) VALUES(?,?,?,?)",
                ("esp32s3-leader-test", "leader-test", owner, platform._now()),
            )
            track_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            payload = {"uuid": track_uuid, "name": "Leader Track", "geo": False,
                       "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]}
            conn.execute(
                """INSERT INTO tracks(device_uid,local_id,name,payload,content_hash,
                   revision,deleted,updated_at,track_uuid,track_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("esp32s3-leader-test", 1, "Leader Track", platform._canonical(payload),
                 "leader-track-hash", 1, 0, platform._now(), track_uuid, 1),
            )
            owner_user = {"id": owner, "username": "leader-owner", "role": "user"}
            sessions = []
            for idx, times in enumerate(([3000, 2500], [2500, 2400], [1000])):
                sid = f"ccccccc{idx}-cccc-4ccc-8ccc-cccccccccccc"
                sessions.append(sid)
                conn.execute(
                    """INSERT INTO sessions(device_uid,local_slot,payload,csv,content_hash,
                       revision,deleted,updated_at,session_uuid,track_uuid,lap_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ("esp32s3-leader-test", idx, "{}", "", f"session-hash-{idx}",
                     1, 0, platform._now(), sid, track_uuid, len(times)),
                )
                for lap_no, lap_ms in enumerate(times, 1):
                    conn.execute(
                        """INSERT INTO lap_records(device_uid,session_uuid,lap_id,lap_number,
                           lap_time_ms,sector_times_ms,best_lap,telemetry_reference,content_hash,
                           deleted,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        ("esp32s3-leader-test", sid, f"lap-{idx}-{lap_no}", lap_no,
                         lap_ms, "[]", 1 if lap_ms == min(times) else 0, "{}",
                         f"lap-hash-{idx}-{lap_no}", 0, platform._now()),
                    )
        public_id = platform.set_track_sharing(
            "esp32s3-leader-test", track_uuid,
            platform.TrackShareBody(public=True), owner_user,
        )["public_id"]
        platform.set_session_sharing(
            "esp32s3-leader-test", sessions[0],
            platform.SessionShareBody(laps_public=True), owner_user)
        platform.set_session_sharing(
            "esp32s3-leader-test", sessions[1],
            platform.SessionShareBody(laps_public=True), owner_user)

        board = platform.public_leaderboard(public_id)
        self.assertEqual(board["format"], "racebrick-public-leaderboard-v1")
        self.assertEqual([e["lap_time_ms"] for e in board["entries"]], [2400, 2500])
        self.assertEqual([e["rank"] for e in board["entries"]], [1, 2])
        self.assertEqual(board["entries"][0]["driver"], "Alice")
        self.assertNotIn("device_uid", board["entries"][0])
        self.assertNotIn("session_uuid", board["entries"][0])

        with platform.db() as conn:
            platform.set_session_sharing(
                "esp32s3-leader-test", sessions[0],
                platform.SessionShareBody(laps_public=False), owner_user)
        board = platform.public_leaderboard(public_id)
        self.assertEqual([e["lap_number"] for e in board["entries"]], [2])
        self.assertEqual([e["lap_time_ms"] for e in board["entries"]], [2400])


    def test_raw_download_requires_independent_authorization(self):
        with platform.db() as conn:
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                ("raw-owner", platform._hash_password("password123"), "user", platform._now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO devices(uid,name,owner_id,created_at) VALUES(?,?,?,?)",
                ("esp32s3-raw-test", "raw-test", owner, platform._now()),
            )
            track_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            conn.execute(
                """INSERT INTO tracks(device_uid,local_id,name,payload,content_hash,
                   revision,deleted,updated_at,track_uuid,track_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("esp32s3-raw-test", 1, "Raw Track", '{"uuid":"%s"}' % track_uuid,
                 "raw-track-hash", 1, 0, platform._now(), track_uuid, 1),
            )
            session_uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            csv = "Format,3\nSession title,\"Raw Track\"\n"
            csv += "1795000000,0,1,0.1,31.0,121.0,20.0\n"
            csv += "1795000001,0,1,0.2,31.0,121.0,21.0\n"
            conn.execute(
                """INSERT INTO sessions(device_uid,local_slot,payload,csv,content_hash,
                   revision,deleted,updated_at,session_uuid,track_uuid,lap_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("esp32s3-raw-test", 1, "{}", csv, "raw-session-hash",
                 1, 0, platform._now(), session_uuid, track_uuid, 1),
            )
            owner_user = {"id": owner, "username": "raw-owner", "role": "user"}

        public_id = platform.set_track_sharing(
            "esp32s3-raw-test", track_uuid,
            platform.TrackShareBody(public=True), owner_user,
        )["public_id"]
        self.assertEqual(platform.public_raw_sessions(public_id)["sessions"], [])
        with self.assertRaises(platform.HTTPException) as ctx:
            platform.public_raw_download(public_id, "rbs_v1_missing", owner_user)
        self.assertEqual(ctx.exception.status_code, 404)

        with platform.db() as conn:
            share = platform.set_session_sharing(
                "esp32s3-raw-test", session_uuid,
                platform.SessionShareBody(raw_public=True), owner_user)
        self.assertFalse(share["laps_public"])
        self.assertTrue(share["raw_public"])
        raw_sessions = platform.public_raw_sessions(public_id)
        self.assertEqual(len(raw_sessions["sessions"]), 1)
        raw_id = raw_sessions["sessions"][0]["session_public_id"]
        response = platform.public_raw_download(public_id, raw_id, owner_user)
        self.assertEqual(response.media_type, "text/csv; charset=utf-8")
        self.assertEqual(response.headers["x-racebrick-raw-version"], "csv-v3")
        self.assertIn(b"1795000000", bytes(response.body))
        self.assertTrue(response.headers["x-racebrick-sha256"])

        with platform.db() as conn:
            platform.set_session_sharing(
                "esp32s3-raw-test", session_uuid,
                platform.SessionShareBody(raw_public=False), owner_user)
        with self.assertRaises(platform.HTTPException) as ctx:
            platform.public_raw_download(public_id, raw_id, owner_user)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(platform.public_raw_sessions(public_id)["sessions"], [])


if __name__ == "__main__":
    unittest.main()
