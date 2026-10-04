"""随机图暴力校验：find_itinerary 与 DFS 枚举所有可行航路的结果一致。

在同一 (节点, 分钟) 状态空间上 DFS 枚举所有合法路径（等待/出发），
暴力取 (抵达分钟, 完整航段序列) 最小者，与 HTTP 规划结果比对。
"""
import itertools
import os
import random
import tempfile

from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


def _brute(segments, controls, tides, origin, dest, start, window, draft):
    """DFS 枚举全部合法航路，返回 (arrival, seg_id 序列, 出发分钟序列) 最优。

    controls：{seg_id: [(起, 止, 通行分钟), ...]}，进入分钟命中即用该航时。
    """
    best = None

    def travel(sid, base_t, dep):
        for s, e, t in controls.get(sid, ()):
            if s <= dep < e:
                return t
        return base_t

    def dfs(node, minute, seq, deps):
        nonlocal best
        if node == dest:
            cand = (minute, tuple(seq), tuple(deps))
            if best is None or cand < best:
                best = cand
            return
        # 等待到下一分钟（若还在窗口内）
        if minute + 1 <= window:
            dfs(node, minute + 1, seq, deps)
        # 从该节点出发的各航段
        for sid, o, d, t, depth in sorted(segments):
            if o != node:
                continue
            tt = travel(sid, t, minute)
            j = minute + tt
            if j > window:
                continue
            # 占用的每一分钟水深须满足（无误差带：depth + tide >= draft）
            if all(depth + tides[m] >= draft for m in range(minute, j)):
                dfs(d, j, seq + [sid], deps + [minute])

    dfs(origin, 0, [], [])
    return best


def test_random_graphs_match_bruteforce(client, monkeypatch):
    from app import config
    rng = random.Random(20261004)
    configs = [
        (["A", "B", "C", "D"], 0.45, [6, 8, 10]),
        (["A", "B", "C", "D", "E"], 0.28, [6, 8]),
        (["A", "B", "C", "D"], 0.7, [6]),
    ]
    seed = 0
    tmpdir = tempfile.mkdtemp(prefix="tide-brute-")
    for nodes, p, windows in configs:
        for _ in range(25):
            # 每轮一个全新库
            db = os.path.join(tmpdir, f"brute_{os.getpid()}_{seed}.db")
            monkeypatch.setattr(config, "DB_PATH", db)
            conn = connect()
            init_db(conn)
            window = rng.choice(windows)
            for n in nodes:
                conn.execute("INSERT INTO nodes(node_id) VALUES (?)", (n,))
            segments = []
            controls = {}
            sid = 0
            for o, d in itertools.permutations(nodes, 2):
                if rng.random() < p:
                    t = rng.randint(1, 2)
                    depth = rng.choice([0.0, 1.0, 99.0])
                    sid += 1
                    conn.execute(
                        "INSERT INTO segments(origin, destination, travel_time,"
                        " depth, clearance) VALUES (?,?,?,?,NULL)",
                        (o, d, t, depth),
                    )
                    segments.append((sid, o, d, t, depth))
                    # 约 1/3 的航段带分时管制：某分钟航时 +1（晚出发可能早到）
                    if rng.random() < 0.35 and window >= 4:
                        c0 = rng.randint(0, window - 3)
                        ct = t + rng.choice([-1, 1])
                        if ct < 1:
                            ct = 2
                        ctl = [(c0, c0 + 1, ct)]
                        controls[sid] = ctl
                        conn.execute(
                            "INSERT INTO segment_travel_controls"
                            " (seg_id, start_minute, end_minute, travel_time)"
                            " VALUES (?,?,?,?)",
                            (sid, c0, c0 + 1, ct),
                        )
            # 每段都有可行窗口的高水位背景 + 随机低潮分钟制造等待
            tides = {m: (0.0 if rng.random() < 0.3 else 5.0) for m in range(window)}
            rev = conn.execute(
                "INSERT INTO tide_revisions(note) VALUES ('r')"
            ).lastrowid
            conn.executemany(
                "INSERT INTO tide_values(revision, minute, level) VALUES (?,?,?)",
                [(rev, m, v) for m, v in tides.items()],
            )
            conn.commit()
            conn.close()

            draft = 1.0
            expected = _brute(segments, controls, tides, "A", "D", 0, window, draft)
            r = client.post("/api/trips/plan", json={
                "origin": "A", "destination": "D",
                "start_minute": 0, "window_minutes": window,
                "draft": draft, "height": 0.0, "margin": 0.0,
                "revision": rev,
            })
            if expected is None:
                assert r.status_code == 404, (seed, r.text)
                seed += 1
                continue
            assert r.status_code == 200, (seed, r.text)
            data = r.json()
            got = (
                data["arrival_minute"],
                tuple(l["seg_id"] for l in data["itinerary"]),
                tuple(l["start_minute"] for l in data["itinerary"]),
            )
            assert got == expected, (seed, got, expected, segments, controls, tides)
            seed += 1
