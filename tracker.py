#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
valorant-tracker-lite: Rank Yoinker互換の超軽量トラッカー (キー不要・CUI)
- VALORANT公式ローカルAPI + pd/glzサーバのみ使用 (Henrikキー不要)
- 依存は requests のみ。rich/colr/DiscordRPC/スキン取得なし
- 表示: ランク, RR, 今Act KD, 前試合 KDA (10人分)
- 軽量化: マッチID変化時のみ取得 / 15秒ポーリング / 詳細キャッシュ

使い方:
  python tracker.py            # 常駐表示 (VALORANT起動中)
  python tracker.py --once     # 1回だけ取得して終了
  python tracker.py --quiet    # 取得中の進行表示なし
  python tracker.py --verbose  # 1人ずつの詳細ログあり
  python tracker.py --no-color # カラーなし
"""
import base64
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib3
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    os.system("")  # WindowsのcmdでANSIカラーを有効化
except Exception:
    pass

if os.name == "nt":
    # 他PCのロケールに依存せずUTF-8出力 (文字化け対策)
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import requests
except ImportError:
    sys.exit("requests が必要です: pip install requests")

# ---------------- 定数 ----------------
RANKS = ["Unrated", "Unused1", "Unused2",
         "Iron 1", "Iron 2", "Iron 3",
         "Bronze 1", "Bronze 2", "Bronze 3",
         "Silver 1", "Silver 2", "Silver 3",
         "Gold 1", "Gold 2", "Gold 3",
         "Platinum 1", "Platinum 2", "Platinum 3",
         "Diamond 1", "Diamond 2", "Diamond 3",
         "Ascendant 1", "Ascendant 2", "Ascendant 3",
         "Immortal 1", "Immortal 2", "Immortal 3",
         "Radiant"]

CLIENT_PLATFORM = ("ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
                   "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
                   "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9")

POLL_SEC = 15
HISTORY_N = 10       # 取得する履歴数
ACT_KD_N = 5         # Act KD計算に使う試合数 (直近N戦)
FETCH_WORKERS = 8    # 並列取得数 (IO待ち用。上げすぎると429が出る)
REQ_TIMEOUT = 6      # 1リクエストの上限秒。引っかかった取得は欠損値で即切上げ
MEM_TTL = 600        # 同一起動中の使い回し秒 (MMR/履歴は試合中に変わらない)
DETAIL_CACHE_MAX = 500  # match_cache.jsonの上限件数

_MISS = object()
_PUUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _slim_details(d):
    """match-detailsからKDA計算に必要な部分だけ残す (570KB→数KB)"""
    try:
        players = d.get("players", []) if isinstance(d, dict) else []
        return {"players": [{"subject": p.get("subject", p.get("Subject")),
                             "stats": p.get("stats", {})} for p in players]}
    except Exception:
        return d

if getattr(sys, "frozen", False):
    # exe化時: キャッシュ類はAPPDATAに保存 (Desktopを汚さない)
    BASE_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "ValorantTrackerLite")
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "match_cache.json")
AGENT_FILE = os.path.join(BASE_DIR, "agents.json")
MAP_FILE = os.path.join(BASE_DIR, "maps.json")
CONTENT_FILE = os.path.join(BASE_DIR, "content.json")
ENCOUNTER_FILE = os.path.join(BASE_DIR, "encounters.json")
MAX_LOG_MATCHES = 100  # 保存する過去マッチ数


def log(msg):
    print(f"[tracker] {msg}", flush=True)


BANNER = r"""
 █  █  █████ █████ █████ █████     ████ █████ █   █
 █  █      █ █   █ █   █ █   █    █     █     ██  █
██████     █ █   █ █   █ █   █    █     █     █ █ █
 █  █  █████ █   █ █   █ █████    █ ███ ████  █ █ █
 █  █  █     █   █ █   █     █    █   █ █     █  ██
██████ █     █   █ █   █     █    █   █ █     █  ██
 █  █  █████ █████ █████ █████     ████ █████ █   █"""


def print_banner():
    print(f"{BOLD}{BANNER}{RESET}", flush=True)


VLT_VERSION = "1.5.2"


# ---------------- lockfile / log ----------------
def get_lockfile():
    path = os.path.join(os.getenv("LOCALAPPDATA", ""),
                        r"Riot Games\Riot Client\Config\lockfile")
    with open(path, encoding="utf-8") as f:
        name, pid, port, password, protocol = f.read().split(":")
    return {"port": port, "password": password, "protocol": protocol}


def parse_shooter_log():
    """ShooterGame.log末尾から region(pd/glz) と ClientVersion を抜く"""
    path = os.path.join(os.getenv("LOCALAPPDATA", ""),
                        r"VALORANT\Saved\Logs\ShooterGame.log")
    pd_region, glz, version = None, None, None
    try:
        # 大きいので末尾5000行だけ読む
        with open(path, encoding="utf8", errors="ignore") as f:
            lines = f.readlines()[-5000:]
    except FileNotFoundError:
        return None, None, None
    for line in lines:
        if ".a.pvp.net/account-xp/v1/" in line and pd_region is None:
            try:
                pd_region = line.split(".a.pvp.net/account-xp/v1/")[0].split(".")[-1]
            except IndexError:
                pass
        # account-xp行が無いfreshなログ用: pd.<region>.a.pvp.net を含む任意のURL行
        if pd_region is None and ".a.pvp.net/" in line and "https://" in line:
            try:
                host = line.split("https://")[1].split("/")[0]
                parts = host.split(".")
                # pd.ap.a.pvp.net / shared.ap.a.pvp.net / glz除外
                if len(parts) >= 4 and parts[-3:] == ["a", "pvp", "net"] \
                        and not parts[0].startswith("glz-"):
                    pd_region = parts[1]
            except IndexError:
                pass
        if "https://glz-" in line and glz is None:
            try:
                seg = line.split("https://glz-")[1].split(".")
                glz = [seg[0], seg[1]]
            except IndexError:
                pass
        if "CI server version:" in line:
            v = line.split("CI server version:")[1].strip().split("\n")[0].strip()
            version = v  # 最後に見つけたものを採用
    return pd_region, glz, version


REGION_CACHE = os.path.join(BASE_DIR, "region_cache.json")


def load_region_cache():
    try:
        with open(REGION_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("pd_region") and d.get("glz"):
            return d["pd_region"], d["glz"]
    except Exception:
        pass
    return None, None


def save_region_cache(pd_region, glz):
    try:
        with open(REGION_CACHE, "w", encoding="utf-8") as f:
            json.dump({"pd_region": pd_region, "glz": glz}, f)
    except Exception:
        pass


def resolve_region(pd_region, glz):
    """ログ→キャッシュ→ap既定の順でregionを決める。起動直後のfreshログでも動く用"""
    if pd_region and glz:
        save_region_cache(pd_region, glz)
        return pd_region, glz, "log"
    c_pd, c_glz = load_region_cache()
    if c_pd and c_glz:
        return c_pd, c_glz, "cache"
    # 日本のユーザ想定の既定値 (外れた場合はAPIがエラーを返すので分かる)
    return "ap", ["ap-1", "ap"], "default(ap)"


def get_client_version_fallback():
    try:
        r = requests.get("https://valorant-api.com/v1/version", timeout=10)
        d = r.json()["data"]
        return d["riotClientVersion"]
    except Exception:
        return None


# ---------------- API client ----------------
class ValoClient:
    def __init__(self):
        self.lock = get_lockfile()
        self.base_local = f"https://127.0.0.1:{self.lock['port']}"
        self.local_auth = {"Authorization": "Basic " + base64.b64encode(
            ("riot:" + self.lock["password"]).encode()).decode()}
        self.pd_region, glz, self.version = parse_shooter_log()
        self.pd_region, glz, region_src = resolve_region(self.pd_region, glz)
        if region_src != "log":
            log(f"regionは{region_src}を使用 (pd={self.pd_region})")
        if self.pd_region == "pbe":
            self.pd_region = "na"
            glz = ["na-1", "na"]
        self.pd = f"https://pd.{self.pd_region}.a.pvp.net"
        self.glz = f"https://glz-{glz[0]}.{glz[1]}.a.pvp.net"
        if not self.version:
            self.version = get_client_version_fallback()
        if not self.version:
            raise RuntimeError("ClientVersion取得失敗。")
        self.ent = self._entitlements()
        self.puuid = self.ent["subject"]
        self.headers = {
            "Authorization": f"Bearer {self.ent['accessToken']}",
            "X-Riot-Entitlements-JWT": self.ent["token"],
            "X-Riot-ClientPlatform": CLIENT_PLATFORM,
            "X-Riot-ClientVersion": self.version,
        }
        self.cache = self._load_cache()
        self._tls = threading.local()  # スレッド毎のkeep-aliveセッション用
        self._mem = {}  # 同一起動中の使い回し (mmr/history/streak)
        self._mem_lock = threading.Lock()

    def _mem_get(self, key):
        with self._mem_lock:
            e = self._mem.get(key)
            if e is not None and time.time() - e[0] < MEM_TTL:
                return e[1]
        return _MISS

    def _mem_set(self, key, value):
        with self._mem_lock:
            self._mem[key] = (time.time(), value)

    def _entitlements(self):
        r = requests.get(self.base_local + "/entitlements/v1/token",
                         headers=self.local_auth, verify=False, timeout=5)
        d = r.json()
        if d.get("message") in ("Entitlements token is not ready yet",) or \
           d.get("errorCode") == "RESOURCE_NOT_FOUND" or "accessToken" not in d:
            raise RuntimeError("VALORANT未起動 (entitlements未発行)。VALORANTを開いてから再実行してください。")
        return d

    def local_get(self, endpoint):
        r = requests.get(self.base_local + endpoint, headers=self.local_auth,
                         verify=False, timeout=5)
        r.raise_for_status()
        return r.json()

    def presence_state(self):
        """本家と同じ方式: local /chat/v4/presences の private をdecodeして
        sessionLoopState(MENUS/PREGAME/INGAME)を返す。取れなければNone"""
        try:
            d = self.local_get("/chat/v4/presences")
        except Exception:
            return None
        presences = d.get("presences", []) if isinstance(d, dict) else []
        for p in presences:
            if p.get("puuid") != self.puuid:
                continue
            priv = p.get("private", "")
            if not priv:
                return None
            try:
                dec = json.loads(base64.b64decode(priv).decode("utf-8", errors="ignore"))
            except Exception:
                return None
            if isinstance(dec, dict):
                if "matchPresenceData" in dec and isinstance(dec["matchPresenceData"], dict):
                    return dec["matchPresenceData"].get("sessionLoopState")
                if "sessionLoopState" in dec:
                    return dec.get("sessionLoopState")
            return None
        return None

    def debug_state(self):
        """--debug用: presenceと各エンドポイントの生状態を表示"""
        out = {}
        try:
            out["presence"] = self.presence_state()
        except Exception as e:
            out["presence"] = f"ERR {e}"
        for name, url in (("pregame", f"/pregame/v1/players/{self.puuid}"),
                          ("coregame", f"/core-game/v1/players/{self.puuid}")):
            try:
                r = requests.get(self.glz + url, headers=self.headers,
                                 verify=False, timeout=10)
                body = (r.text or "")[:200].replace("\n", " ")
                out[name] = f"{r.status_code} {body}"
            except Exception as e:
                out[name] = f"ERR {e}"
        # pregameのチーム構成 (敵が見えるか)
        try:
            pm = self.g(self.glz, f"/pregame/v1/players/{self.puuid}")
            if pm and pm.get("MatchID"):
                d = self.g(self.glz, f"/pregame/v1/matches/{pm['MatchID']}")

                def summ(t):
                    pls = (t or {}).get("Players", []) or []
                    n_subj = sum(1 for p in pls if p.get("Subject"))
                    n_char = sum(1 for p in pls if p.get("CharacterID"))
                    return f"{len(pls)}人 subject={n_subj} char={n_char}"

                out["pregame_teams"] = {k: summ((d or {}).get(k)) for k in ("AllyTeam", "EnemyTeam")}
                out["pregame_Teams"] = [(t.get("TeamID"), len(t.get("Players", []) or []))
                                        for t in (d or {}).get("Teams", []) or []]
            else:
                out["pregame_teams"] = "no pregame"
        except Exception as e:
            out["pregame_teams"] = f"ERR {e}"
        return out

    def raw_status(self, endpoint):
        """診断用: glzエンドポイントの生ステータス"""
        try:
            r = self._sess().request("GET", self.glz + endpoint, timeout=REQ_TIMEOUT)
            return f"{r.status_code} {(r.text or '')[:120]}".replace("\n", " ")
        except Exception as e:
            return f"ERR {e}"

    def _load_cache(self):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            # 旧形式の肥大ボディは読み込み時に削減
            return {k: _slim_details(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_cache(self):
        try:
            if len(self.cache) > DETAIL_CACHE_MAX:  # 肥大防止
                drop = len(self.cache) - DETAIL_CACHE_MAX
                for k in list(self.cache.keys())[:drop]:
                    del self.cache[k]
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
        except Exception:
            pass

    def _sess(self):
        """keep-aliveセッション (TLSハンドシェイク省略で高速化。スレッド毎に分離)"""
        s = getattr(self._tls, "s", None)
        if s is None:
            s = requests.Session()
            s.verify = False
            s.headers.update(self.headers)
            self._tls.s = s
        return s

    def g(self, base, endpoint, method="GET", **kw):
        kw.setdefault("timeout", REQ_TIMEOUT)
        r = self._sess().request(method, base + endpoint, **kw)
        if r.status_code in (400, 404, 503):
            # そのフェーズにいない等 (試合終了直後の古いpresence等) -> 正常系として扱う
            return None
        r.raise_for_status()
        return r.json() if r.text else None

    # --- state ---
    # NOTE: coregame系はハイフン付き /core-game/v1/... が正しい (本家準拠)。
    # /coregame/... (ハイフン無し) は503になる。
    def current_match(self):
        """(phase, match_id) を返す。phase: PREGAME/INGAME/MENU"""
        # INGAMEを最優先で直接確認 (古いpregame残留・presence遅延に引きずられない)
        try:
            core = self.g(self.glz, f"/core-game/v1/players/{self.puuid}")
        except Exception:
            core = None
        if core and core.get("MatchID"):
            return "INGAME", core["MatchID"]
        try:
            st = self.presence_state()
        except Exception:
            st = None
        if st == "MENUS":
            return "MENU", None
        # PREGAME or 不明: pregameを確認
        try:
            pre = self.g(self.glz, f"/pregame/v1/players/{self.puuid}")
        except Exception:
            pre = None
        if pre and pre.get("MatchID"):
            return "PREGAME", pre["MatchID"]
        return "MENU", None

    def pregame_players(self, mid):
        d = self.g(self.glz, f"/pregame/v1/matches/{mid}")
        if not d:
            return []
        ally = (d.get("AllyTeam") or {}).get("Players", []) or []
        enemy = (d.get("EnemyTeam") or {}).get("Players", []) or []
        if not enemy:
            # EnemyTeamが無い場合: Teamsから自チーム以外を敵として扱う
            ally_ids = {p.get("Subject") for p in ally}
            for t in d.get("Teams", []) or []:
                pls = t.get("Players", []) or []
                if pls and pls[0].get("Subject") not in ally_ids:
                    enemy = pls
                    break

        def conv(p, team):
            return {"puuid": p.get("Subject"), "team": team,
                    "agent": p.get("CharacterID", "") or "",
                    "level": (p.get("PlayerIdentity") or {}).get("AccountLevel")}

        out = [conv(p, "Ally") for p in ally] + [conv(p, "Enemy") for p in enemy]
        # 最終手段: TeamMatchToken(JWT)の参加者IDから漏れを補完
        known = {p["puuid"] for p in out}
        for s in token_puuids(d.get("TeamMatchToken")):
            if s not in known:
                out.append({"puuid": s, "team": "Enemy", "agent": "", "level": None})
                known.add(s)
        return out

    def pregame_count(self, mid):
        """pregameのユニーク人数。敵後出し検出用 (本家は状態変化時のみ再取得)"""
        try:
            d = self.g(self.glz, f"/pregame/v1/matches/{mid}")
        except Exception:
            return None
        if not d:
            return None
        teams = d.get("Teams", []) or []
        if teams:
            subs = {p.get("Subject") for t in teams for p in (t.get("Players", []) or [])}
        else:
            subs = set()
            for key in ("AllyTeam", "EnemyTeam"):
                for p in ((d.get(key) or {}).get("Players", []) or []):
                    subs.add(p.get("Subject"))
        subs.discard(None)
        subs.discard("")
        return len(subs)

    def coregame_players(self, mid):
        d = self.g(self.glz, f"/core-game/v1/matches/{mid}")
        if not d:
            return []
        out = []
        for p in d.get("Players", []):
            out.append({"puuid": p.get("Subject"), "team": p.get("TeamID", ""),
                        "agent": p.get("CharacterID", ""),
                        "level": (p.get("PlayerIdentity") or {}).get("AccountLevel")})
        return out

    # --- rank / names / history (同一起動中は使い回し。試合中に変わらないため) ---
    def mmr(self, puuid):
        key = f"mmr:{puuid}"
        hit = self._mem_get(key)
        if hit is not _MISS:
            return hit
        res = self._mmr_fetch(puuid)
        self._mem_set(key, res)
        return res

    def _mmr_fetch(self, puuid):
        try:
            d = self.g(self.pd, f"/mmr/v1/players/{puuid}")
        except Exception:
            return {"rank": "Unrated", "rr": 0, "season": None, "tier": 0,
                    "peak_tier": 0, "peak_season": None}
        if not d:
            return {"rank": "Unrated", "rr": 0, "season": None, "tier": 0,
                    "peak_tier": 0, "peak_season": None}
        try:
            latest = d.get("LatestCompetitiveUpdate") or {}
            season = latest.get("SeasonID")
            q = (d.get("QueueSkills") or {}).get("competitive") or {}
            info = (q.get("SeasonalInfoBySeasonID") or {}).get(season or "", {})
            tier = info.get("CompetitiveTier", 0)
            rr = info.get("RankedRating", 0)
            # 最高ランク: 全シーズンのWinsByTierの最大値 (本家と同方式)
            peak_tier, peak_season = tier, season
            try:
                best = -1
                infos = q.get("SeasonalInfoBySeasonID") or {}
                for sid, sinfo in infos.items():
                    wins = (sinfo or {}).get("WinsByTier")
                    if not wins:
                        continue
                    for w in wins:
                        try:
                            t = int(w)
                        except (TypeError, ValueError):
                            continue
                        if t > best:
                            best, peak_season = t, sid
                if best >= 0:
                    peak_tier = best
            except Exception:
                pass
            return {"rank": RANKS[tier] if 0 <= tier < len(RANKS) else str(tier),
                    "rr": rr, "season": season, "tier": tier,
                    "peak_tier": peak_tier, "peak_season": peak_season}
        except Exception:
            return {"rank": "?", "rr": 0, "season": None, "tier": 0,
                    "peak_tier": 0, "peak_season": None}

    def names(self, puuids):
        try:
            r = self._sess().request("PUT", self.pd + "/name-service/v2/players",
                                     headers={"Content-Type": "application/json"},
                                     timeout=REQ_TIMEOUT, json=list(set(puuids)))
            arr = r.json()
            return {x.get("Subject"): f"{x.get('GameName')}#{x.get('TagLine')}" for x in arr}
        except Exception:
            return {p: p[:8] for p in puuids}

    def history(self, puuid):
        key = f"hist:{puuid}"
        hit = self._mem_get(key)
        if hit is not _MISS:
            return hit
        res = self._history_fetch(puuid)
        self._mem_set(key, res)
        return res

    def _history_fetch(self, puuid):
        try:
            d = self.g(self.pd, f"/match-history/v1/history/{puuid}?startIndex=0&endIndex={HISTORY_N}")
        except Exception:
            return []
        return (d or {}).get("History", []) if d else []

    def streak(self, puuid, n=8):
        """直近コンペの連勝/連敗を W3 / L2 形式で返す。不明時は -"""
        key = f"streak:{puuid}:{n}"
        hit = self._mem_get(key)
        if hit is not _MISS:
            return hit
        res = self._streak_fetch(puuid, n)
        self._mem_set(key, res)
        return res

    def _streak_fetch(self, puuid, n=8):
        """直近コンペの連勝/連敗を W3 / L2 形式で返す。不明時は -"""
        try:
            d = self.g(self.pd, f"/mmr/v1/players/{puuid}/competitiveupdates"
                                f"?startIndex=0&endIndex={n}&queue=competitive")
        except Exception:
            return "-"
        matches = (d or {}).get("Matches", []) if d else []

        def res(m):
            try:
                rr = int(m.get("RankedRatingEarned", 0))
            except (TypeError, ValueError):
                return 0
            return 1 if rr > 0 else (-1 if rr < 0 else 0)

        if not matches:
            return "-"
        first = res(matches[0])
        if first == 0:
            return "-"
        c = 0
        for m in matches:
            if res(m) == first:
                c += 1
            else:
                break
        return f"W{c}" if first > 0 else f"L{c}"

    def details(self, mid):
        if mid in self.cache:
            return self.cache[mid]
        try:
            d = self.g(self.pd, f"/match-details/v1/matches/{mid}")
        except Exception:
            return None
        if d:
            self.cache[mid] = _slim_details(d)
        return self.cache.get(mid)

    @staticmethod
    def kd_of(details, puuid):
        try:
            for p in details.get("players", []):
                if p.get("subject") == puuid or p.get("Subject") == puuid:
                    s = p.get("stats") or {}
                    return s.get("kills", 0), s.get("deaths", 0), s.get("assists", 0)
        except Exception:
            pass
        return None


# ---------------- agents ----------------
def agent_short_map():
    if os.path.exists(AGENT_FILE):
        try:
            with open(AGENT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        r = requests.get("https://valorant-api.com/v1/agents?isPlayableCharacter=true", timeout=15)
        m = {a["uuid"].lower(): a["displayName"] for a in r.json()["data"]}
        with open(AGENT_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        return m
    except Exception:
        return {}


# ---------------- stats ----------------
def hget(h, *names):
    """履歴エントリのキーを大文字小文字無視で取得 (SeasonID/SeasonId等ゆれ対策)"""
    if not isinstance(h, dict):
        return None
    low = {k.lower(): v for k, v in h.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def token_puuids(token):
    """TeamMatchToken(JWT)の参加者IDを抜く。EnemyTeam非表示時の補完用"""
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return []
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out = []

    def walk(o):
        if isinstance(o, str):
            if _PUUID_RE.fullmatch(o):
                out.append(o.lower())
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for x in o.values():
                walk(x)

    walk(d)
    return out


def player_stats(cli, puuid, season=None):
    hist = cli.history(puuid) or []
    prev_kda, act_kd, kd_num, kd_n = "-", "-", 0.0, 0
    # 前試合: 直近1件
    if hist:
        mid = hget(hist[0], "MatchID", "MatchId", "matchID") or ""
        det = cli.details(mid) if mid else None
        if det:
            kd = cli.kd_of(det, puuid)
            if kd:
                prev_kda = f"{kd[0]}/{kd[1]}/{kd[2]}"
    # 今Act: 直近コンペのシーズン(=現Act)を基準に直近ACT_KD_N戦で平均
    comp = [h for h in hist
            if "compet" in str(hget(h, "QueueID", "QueueId", "queueID") or "").lower()]
    cur_season = hget(comp[0], "SeasonID", "SeasonId", "seasonID") if comp else None
    tk = td = n = 0
    for h in comp:
        if n >= ACT_KD_N:
            break
        if cur_season and hget(h, "SeasonID", "SeasonId", "seasonID") != cur_season:
            continue
        mid = hget(h, "MatchID", "MatchId", "matchID") or ""
        det = cli.details(mid) if mid else None
        if not det:
            continue
        kd = cli.kd_of(det, puuid)
        if kd:
            tk += kd[0]
            td += kd[1]
            n += 1
    if n > 0:
        kd_num = tk / max(td, 1)
        kd_n = n
        act_kd = f"{kd_num:.2f} ({n}戦)"
    return prev_kda, act_kd, kd_num, kd_n


# ---------------- vandal skins (smurf対策) ----------------
WEAPON_FILE = os.path.join(BASE_DIR, "weapons_vandal.json")
SKIN_SOCKET = "bcef87d6-209b-46c6-8b19-fbe40bd95abc"  # 本家準拠のスキンソケット
VERBOSE = "--verbose" in sys.argv
QUIET = "--quiet" in sys.argv


def vandal_skin_map():
    """(vandalのweapon uuid, {skin uuid: (短縮名, tier色hex)}) を返す。キャッシュ優先"""
    if os.path.exists(WEAPON_FILE):
        try:
            with open(WEAPON_FILE, encoding="utf-8") as f:
                d = json.load(f)
            skins = {}
            for k, v in d["skins"].items():
                if isinstance(v, str):  # 旧形式 {uuid: 名前}
                    skins[k] = (v, None)
                else:
                    skins[k] = (v.get("name", "?"), v.get("color"))
            return d["vandal_uuid"], skins
        except Exception:
            pass
    try:
        tier_colors = {}
        try:
            r = requests.get("https://valorant-api.com/v1/contenttiers", timeout=15)
            for t in r.json()["data"]:
                c = (t.get("highlightColor") or "")[:6]
                if len(c) == 6:
                    tier_colors[t["uuid"].lower()] = c
        except Exception:
            pass
        r = requests.get("https://valorant-api.com/v1/weapons", timeout=15)
        for w in r.json()["data"]:
            if w.get("displayName") == "Vandal":
                skins = {}
                for s in w.get("skins", []):
                    name = s.get("displayName", "").replace(" Vandal", "")
                    if name.lower().startswith("standard"):
                        name = "STD"
                    color = tier_colors.get((s.get("contentTierUuid") or "").lower())
                    skins[s["uuid"].lower()] = {"name": name, "color": color}
                d = {"vandal_uuid": w["uuid"].lower(), "skins": skins}
                with open(WEAPON_FILE, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
                return d["vandal_uuid"], {k: (v["name"], v["color"]) for k, v in skins.items()}
    except Exception:
        pass
    return None, {}


def match_vandal_skins(cli, phase, mid):
    """マッチの装備から {puuid小文字: (Vandalスキン名, tier色hex)} を返す。1リクエストのみ"""
    try:
        ep = (f"/pregame/v1/matches/{mid}/loadouts" if phase == "PREGAME"
              else f"/core-game/v1/matches/{mid}/loadouts")
        d = cli.g(cli.glz, ep)
        entries = (d or {}).get("Loadouts", [])
        vandal_uuid, skin_names = vandal_skin_map()
        if not vandal_uuid:
            return {}
        out = {}
        for e in entries:
            subj = (e.get("Subject") or "").lower()
            if not subj:
                continue
            inv = e.get("Loadout", e)  # gameはLoadoutキー、pregameは直下
            items = inv.get("Items", {}) if isinstance(inv, dict) else {}
            for wuuid, w in items.items():
                if str(wuuid).lower() != vandal_uuid:
                    continue
                try:
                    sid = w["Sockets"][SKIN_SOCKET]["Item"]["ID"]
                except (KeyError, TypeError):
                    continue
                out[subj] = skin_names.get(str(sid).lower(), ("?", None))
        return out
    except Exception:
        return {}


def smurf_mark(level, tier, kd_num, kd_n):
    """低Lvかつ高ランク/高KDなら ! を返す (目安であり確定ではない)"""
    if level is None:
        return ""
    try:
        if int(level) < 40 and (tier >= 18 or (kd_n >= 3 and kd_num >= 1.6)):
            return "!"
    except (TypeError, ValueError):
        pass
    return ""


# ---------------- peak rank (最高ランク+時期) ----------------
def content_seasons(cli, max_age_days=3):
    """content-serviceのシーズン一覧。キャッシュ優先 (3日で更新)"""
    try:
        with open(CONTENT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - d.get("time", 0) < max_age_days * 86400 and d.get("seasons"):
            return d["seasons"]
    except Exception:
        pass
    try:
        d = cli.g(f"https://shared.{cli.pd_region}.a.pvp.net", "/content-service/v3/content")
        seasons = [{"ID": s.get("ID"), "Name": s.get("Name"), "Type": s.get("Type")}
                   for s in (d or {}).get("Seasons", [])]
        if seasons:
            with open(CONTENT_FILE, "w", encoding="utf-8") as f:
                json.dump({"time": time.time(), "seasons": seasons}, f)
            return seasons
    except Exception:
        pass
    try:
        with open(CONTENT_FILE, encoding="utf-8") as f:
            return json.load(f).get("seasons", [])
    except Exception:
        return []


def _roman(tok):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total, prev = 0, 0
    for ch in reversed(str(tok).upper()):
        if ch not in vals:
            return None
        cur = vals[ch]
        total, prev = (total - cur, cur) if cur < prev else (total + cur, cur)
    return total


def _season_num(name, is_episode):
    if not name or not isinstance(name, str):
        return None
    tok = name.split()[-1]
    if any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
        return tok.upper()
    if is_episode:
        try:
            return int(tok)
        except ValueError:
            return _roman(tok)
    r = _roman(tok)
    if r is not None:
        return r
    try:
        return int(tok)
    except ValueError:
        return None


def act_episode(seasons, act_id):
    """actのシーズンID -> (episode, act)。本家のget_act_episode_from_act_id相当"""
    last_ep, ep, act = None, None, None
    for s in seasons or []:
        if s.get("Type") == "episode":
            last_ep = s.get("Name")
        if (s.get("ID") or "").lower() == (act_id or "").lower():
            act = _season_num(s.get("Name"), False)
            ep = _season_num(last_ep, True)
    return ep, act


def short_rank(tier):
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        return "?"
    if tier <= 2:
        return "-"
    if tier >= len(RANKS):
        return "?"
    name = RANKS[tier]
    if name == "Radiant":
        return "Radiant"
    try:
        base, num = name.rsplit(" ", 1)
    except ValueError:
        return name
    abbr = {"Iron": "I", "Bronze": "B", "Silver": "S", "Gold": "G",
            "Platinum": "P", "Diamond": "D", "Ascendant": "A",
            "Immortal": "Im"}.get(base, base)
    return f"{abbr}{num}"


def peak_label(seasons, tier, season_id):
    if not tier or tier <= 2:
        return "-"
    s = short_rank(tier)
    ep, act = act_episode(seasons, season_id) if season_id else (None, None)
    if ep is None or act is None:
        return s
    if isinstance(ep, int) and isinstance(act, int):
        return f"{s} E{ep}A{act}"
    return f"{s} {ep}A{act}"


# ---------------- encounters (過去の同席) ----------------
def load_match_log():
    try:
        with open(ENCOUNTER_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("matches"), list):
            return d["matches"]
    except Exception:
        pass
    return []


def append_match_log(mid, map_name, rows):
    try:
        matches = load_match_log()
        matches = [m for m in matches if m.get("mid") != mid]
        matches.append({"mid": mid, "time": time.time(), "map": map_name or "",
                        "players": [{"puuid": r["puuid"], "name": r["name"],
                                     "team": r["team"]} for r in rows]})
        matches = matches[-MAX_LOG_MATCHES:]
        with open(ENCOUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump({"matches": matches}, f, ensure_ascii=False)
    except Exception:
        pass


def find_encounters(mid, self_puuid, puuids, name_of):
    """過去マッチにいたプレイヤーを [{name, times, last}] で返す (自分・現マッチ除外)"""
    out = []
    for puuid in dict.fromkeys(puuids):
        if puuid == self_puuid:
            continue
        past = [m for m in load_match_log()
                if m.get("mid") != mid
                and any(p.get("puuid") == puuid for p in m.get("players", []))]
        if past:
            last = max(m.get("time", 0) for m in past)
            out.append({"name": name_of.get(puuid, puuid[:8]),
                        "times": len(past), "last": last})
    out.sort(key=lambda e: (-e["times"], -e["last"]))
    return out


def show_encounters(enc):
    if not enc:
        return
    print("  再戦")
    for e in enc:
        day = time.strftime("%m/%d", time.localtime(e["last"]))
        print(f"  {e['name']} 過去{e['times']}回 前回{day}")


# ---------------- UI ----------------
USE_COLOR = ("--no-color" not in sys.argv) and (os.environ.get("NO_COLOR") is None)


def C(code):
    return f"\033[{code}m" if USE_COLOR else ""


RESET, BOLD, DIM = C("0"), C("1"), C("2")


def truecolor(hex6):
    try:
        h = (hex6 or "").strip().lstrip("#")
        if len(h) < 6:
            return ""
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[38;2;{r};{g};{b}m" if USE_COLOR else ""
    except (ValueError, TypeError):
        return ""


TEAM_COLORS = {"Blue": C("94"), "Red": C("91"), "Ally": C("94"), "Enemy": C("91")}
RANK_COLORS = [("Radiant", C("93")), ("Immortal", C("91")), ("Ascendant", C("92")),
               ("Diamond", C("95")), ("Platinum", C("96")), ("Gold", C("33")),
               ("Silver", C("37")), ("Bronze", truecolor("cd7f32")), ("Iron", C("90"))]


def rank_color(rank):
    for prefix, code in RANK_COLORS:
        if rank.startswith(prefix):
            return code
    return C("90") if rank == "Unrated" else ""


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in str(s))


def pad(s, w):
    s = str(s)
    fill = max(0, w - disp_width(s))
    return s + " " * fill


def trunc(s, w):
    s = str(s)
    out, cur = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if cur + cw > w:
            break
        out += ch
        cur += cw
    return out


def head_color_cell(text, w, color):
    """先頭語だけ色付け、残り(数字・時期)は白。'Diamond 1' 'Im3 E7A3'用"""
    text = trunc(text, w)
    if " " in text:
        a, b = text.split(" ", 1)
        return f"{color}{a}{RESET} {pad(b, w - disp_width(a) - 1)}"
    return f"{color}{pad(text, w)}{RESET}"


def is_anon(raw):
    g = (raw or "").split("#")[0]
    return (not g) or g == "#"


def display_name(raw, agent):
    if is_anon(raw):
        return agent if agent else "Player"
    return raw


def map_name_map():
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        r = requests.get("https://valorant-api.com/v1/maps", timeout=15)
        m = {a["mapUrl"]: a["displayName"] for a in r.json()["data"]}
        with open(MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        return m
    except Exception:
        return {}


def match_map_name(cli, phase, mid):
    try:
        d = cli.g(cli.glz, f"/pregame/v1/matches/{mid}" if phase == "PREGAME"
                  else f"/core-game/v1/matches/{mid}")
        map_id = (d or {}).get("MapID", "")
        if not map_id:
            return ""
        maps = map_name_map()
        if map_id in maps:
            return maps[map_id]
        return map_id.strip("/").split("/")[-1]  # /Game/Maps/Ascent/Ascent -> Ascent
    except Exception:
        return ""


def show(rows, phase, mid, me=None, map_name=""):
    W = {"team": 5, "lv": 5, "name": 18, "agent": 8, "rank": 12, "rr": 4,
         "actkd": 11, "vandal": 12, "prev": 9, "streak": 6, "peak": 12}
    # 広い端末ではName/Vandalを伸ばして省略を減らす
    try:
        import shutil
        term_w = shutil.get_terminal_size((120, 30)).columns
    except Exception:
        term_w = 120
    base_content = 113  # 上記Wでの内容幅
    extra = max(0, term_w - base_content - 4)
    add_name = min(extra * 6 // 10, 10)
    W["name"] += add_name
    W["vandal"] += min(extra - add_name, 8)
    width = base_content + (W["name"] - 18) + (W["vandal"] - 12) + 3
    title = f" {phase}"
    if map_name:
        title += f"  map={map_name}"
    title += f"  ({len(rows)}人)"
    print(f"\n{BOLD}{'=' * width}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    head = (f" {pad('Team', W['team'])} {pad('Lv', W['lv'])} {pad('Name', W['name'])} "
            f"{pad('Agent', W['agent'])} {pad('Rank', W['rank'])} {pad('RR', W['rr'])}  "
            f"{pad('ActKD', W['actkd'])} {pad('Vandal', W['vandal'])} "
            f"{pad('Prev', W['prev'])} {pad('Streak', W['streak'])} {pad('Peak', W['peak'])}")
    print(f"{DIM}{head}{RESET}")
    print(f"{DIM}{'-' * width}{RESET}")
    last_team = None
    for r in rows:
        if last_team is not None and r["team"] != last_team:
            print()
        last_team = r["team"]
        tc = TEAM_COLORS.get(r["team"], "")
        rc = rank_color(r["rank"])
        is_me = me and r.get("puuid") == me
        you = "▶" if is_me else " "
        lv_cell = pad(r["level"], W["lv"])
        if r.get("smurf"):
            lv_cell = f"{C('93')}{lv_cell}{RESET}"
        st = r.get("streak", "-")
        if st.startswith("W"):
            st = f"{C('92')}{pad(st, W['streak'])}{RESET}"
        elif st.startswith("L"):
            st = f"{C('91')}{pad(st, W['streak'])}{RESET}"
        else:
            st = pad(st, W["streak"])
        vcell = pad(trunc(r["vandal"], W["vandal"]), W["vandal"])
        vc = truecolor(r.get("vandal_color"))
        if vc:
            vcell = f"{vc}{vcell}{RESET}"
        elif r["vandal"] == "STD":
            vcell = f"{DIM}{vcell}{RESET}"
        pt = r.get("peak_tier", 0)
        try:
            pname = RANKS[int(pt)] if 0 <= int(pt) < len(RANKS) else ""
        except (TypeError, ValueError):
            pname = ""
        pcell = head_color_cell(r.get("peak", "-"), W["peak"], rank_color(pname))
        line = (f"{you}{tc}{pad(r['team'], W['team'])}{RESET} {lv_cell} "
                f"{BOLD if is_me else ''}{pad(trunc(r['name'], W['name']), W['name'])}{RESET} "
                f"{pad(trunc(r['agent'], W['agent']), W['agent'])} "
                f"{head_color_cell(r['rank'], W['rank'], rc)} "
                f"{pad(r['rr'], W['rr'])}  {pad(r['actkd'], W['actkd'])} "
                f"{vcell} "
                f"{pad(r['prev'], W['prev'])} {st} {pcell}")
        print(line)
    print(f"{BOLD}{'=' * width}{RESET}\n", flush=True)


def build_rows(cli, phase, mid, players, agents):
    total = len(players)
    done = [0]
    lock = threading.Lock()

    def tick(name=""):
        if VERBOSE:
            if name:
                print(f"  {name}", flush=True)
            return
        if QUIET:
            return
        with lock:
            done[0] += 1
            print(f"\r  取得中 {done[0]}/{total}...", end="", flush=True)

    def core(p):
        """重い取得だけ先行 (名前解決・見た目の組立ては後で一括)"""
        try:
            m = cli.mmr(p["puuid"])
            prev_kda, act_kd, kd_num, kd_n = player_stats(cli, p["puuid"], m["season"])
            streak = cli.streak(p["puuid"])
            tick(f"{p['puuid'][:8]} {m['rank']} ActKD={act_kd} Prev={prev_kda}" if VERBOSE else "")
            return (p, m, prev_kda, act_kd, kd_num, kd_n, streak)
        except Exception:
            tick()
            return (p, {"rank": "?", "rr": 0, "tier": 0, "peak_tier": 0, "peak_season": None},
                    "-", "-", 0.0, 0, "-")

    # 名前・スキン・シーズン表も同時取得 (直列3RTTを削減)
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, total + 3) if total else 1) as ex:
        f_names = ex.submit(cli.names, [p["puuid"] for p in players])
        f_vandal = ex.submit(match_vandal_skins, cli, phase, mid)
        f_seasons = ex.submit(content_seasons, cli)
        cores = list(ex.map(core, players))
        name_map = f_names.result()
        vandal = f_vandal.result()
        seasons = f_seasons.result()
    if not VERBOSE and not QUIET:
        print("\r  取得完了            ", flush=True)

    rows = []
    for (p, m, prev_kda, act_kd, kd_num, kd_n, streak) in cores:
        agent = agents.get((p["agent"] or "").lower(), (p["agent"] or "")[:6])
        raw_name = (name_map or {}).get(p["puuid"], p["puuid"][:8])
        anon = is_anon(raw_name)
        lv = p.get("level")
        if not lv:
            lv = None  # 0/Noneは非公開扱い
        mark = "" if (anon or lv is None) else smurf_mark(lv, m["tier"], kd_num, kd_n)
        vname, vcolor = (vandal or {}).get(p["puuid"].lower(), ("-", None))
        rows.append({"puuid": p["puuid"], "team": p["team"],
                     "name": agent if anon else raw_name,
                     "agent": agent, "rank": m["rank"], "rr": m["rr"],
                     "actkd": act_kd, "prev": prev_kda,
                     "level": "-" if lv is None else f"{lv}{mark}",
                     "smurf": bool(mark),
                     "vandal": vname, "vandal_color": vcolor,
                     "streak": streak,
                     "peak": peak_label(seasons, m["peak_tier"], m["peak_season"]),
                     "peak_tier": m["peak_tier"]})
    # 自チームを先に
    my_team = next((p["team"] for p in players if p["puuid"] == cli.puuid), None)
    if my_team:
        rows.sort(key=lambda r: 0 if r["team"] == my_team else 1)
    cli._save_cache()
    return rows


def run_once():
    try:
        cli = ValoClient()
    except Exception as e:
        log(f"{e}")
        return
    if "--debug" in sys.argv:
        log(f"debug={cli.debug_state()}")
    log(f"pd={cli.pd} version={cli.version[:24]}...")
    try:
        phase, mid = cli.current_match()
    except Exception as e:
        log(f"{e}")
        return
    if phase == "MENU":
        log("MENU状態 (プレゲーム/試合中ではありません)。エージェント選択か試合中に実行してください。")
        return
    agents = agent_short_map()
    players = cli.pregame_players(mid) if phase == "PREGAME" else cli.coregame_players(mid)
    log(f"{phase} {len(players)}人取得")
    rows = build_rows(cli, phase, mid, players, agents)
    mmap = match_map_name(cli, phase, mid)
    show(rows, phase, mid, me=cli.puuid, map_name=mmap)
    show_encounters(find_encounters(mid, cli.puuid, [r["puuid"] for r in rows],
                                    {r["puuid"]: r["name"] for r in rows}))
    append_match_log(mid, mmap, rows)


def main():
    print_banner()
    log(f"ValorantTrackerLite v{VLT_VERSION}")
    once = "--once" in sys.argv
    if once:
        run_once()
        return
    changed = threading.Event()
    stop = threading.Event()
    if HAS_WS:
        PresenceWatcher(changed, stop).start()
    else:
        log("websocket-clientが無いためポーリングのみ (pip install websocket-client)")
    last_mid = None
    fail_count = 0
    pregame_since = None
    pregame_diag_done = False
    pregame_count = None

    def wait_awhile():
        changed.wait(POLL_SEC)  # presence変化で即起床、無ければPOLL_SEC待機
        changed.clear()

    while True:
        try:
            cli = ValoClient()
            fail_count = 0
        except Exception as e:
            log(f"{e} -> {POLL_SEC}秒後に再試行")
            wait_awhile()
            continue
        try:
            while True:
                try:
                    phase, mid = cli.current_match()
                    fail_count = 0
                except Exception as e:
                    fail_count += 1
                    log(f"取得失敗 ({e})、{POLL_SEC}秒後に再試行します")
                    if fail_count >= 3:
                        log("接続を作り直します")
                        break  # 外側でValoClient再作成 (lockfile再読込)
                    wait_awhile()
                    continue
                if phase == "MENU":
                    if last_mid != "MENU":
                        log("待機中: エージェント選択/試合開始を待っています…")
                        last_mid = "MENU"
                    pregame_since = None
                    pregame_count = None
                    wait_awhile()
                    continue
                if mid == last_mid:
                    if phase == "PREGAME":
                        # 敵後出し等で人数が増えたら再取得
                        try:
                            n = cli.pregame_count(mid)
                        except Exception:
                            n = None
                        if n is not None and pregame_count is not None and n > pregame_count:
                            log(f"PREGAME {pregame_count}人→{n}人、再取得します")
                            pregame_count = n
                            pregame_since = None
                        else:
                            if pregame_count is None and n is not None:
                                pregame_count = n
                            # 同じpregameに長居したら診断 (選択は最大2分弱のはず)
                            now = time.time()
                            if pregame_since is None:
                                pregame_since = now
                                pregame_diag_done = False
                            elif not pregame_diag_done and now - pregame_since > 240:
                                pregame_diag_done = True
                                try:
                                    ps = cli.presence_state()
                                except Exception as e:
                                    ps = f"ERR {e}"
                                log(f"診断 presence={ps} "
                                    f"core={cli.raw_status(f'/core-game/v1/players/{cli.puuid}')} "
                                    f"pre={cli.raw_status(f'/pregame/v1/players/{cli.puuid}')}")
                            wait_awhile()
                            continue
                    else:
                        wait_awhile()
                        continue
                pregame_since = None
                last_mid = mid
                log(f"{phase}検出 ({mid[:8]}…)、情報を取得します…")
                n_fetched = run_once_inner(cli, phase, mid)
                pregame_count = n_fetched if phase == "PREGAME" else None
        except KeyboardInterrupt:
            print("\n終了します。")
            try:
                stop.set()
            except Exception:
                pass
            return


def run_once_inner(cli, phase, mid):
    agents = agent_short_map()
    players = cli.pregame_players(mid) if phase == "PREGAME" else cli.coregame_players(mid)
    log(f"{phase} {len(players)}人取得")
    rows = build_rows(cli, phase, mid, players, agents)
    mmap = match_map_name(cli, phase, mid)
    show(rows, phase, mid, me=cli.puuid, map_name=mmap)
    show_encounters(find_encounters(mid, cli.puuid, [r["puuid"] for r in rows],
                                    {r["puuid"]: r["name"] for r in rows}))
    append_match_log(mid, mmap, rows)
    return len(players)


# ---------------- presence watcher (本家式websocket即時検知) ----------------
try:
    import websocket as ws_client
    HAS_WS = True
except ImportError:
    ws_client = None
    HAS_WS = False


def _is_presence_msg(m):
    try:
        if not m or len(m) <= 10:
            return False
        d = json.loads(m)
        return d[2].get("uri") == "/chat/v4/presences"
    except Exception:
        return False


class PresenceWatcher(threading.Thread):
    """wss://127.0.0.1:portを購読しpresence変化でchangedを立てる。切断時は自動再接続"""

    def __init__(self, changed, stop):
        super().__init__(daemon=True)
        self.changed = changed
        self.stop = stop

    def run(self):
        import ssl as _ssl
        backoff = 2
        while not self.stop.is_set():
            try:
                lock = get_lockfile()
            except Exception:
                if self.stop.wait(10):
                    return
                continue
            ws = None
            try:
                auth = "Basic " + base64.b64encode(
                    ("riot:" + lock["password"]).encode()).decode()
                ws = ws_client.create_connection(
                    f"wss://127.0.0.1:{lock['port']}",
                    timeout=10, header={"Authorization": auth},
                    sslopt={"cert_reqs": _ssl.CERT_NONE})
                ws.settimeout(5)
                ws.send('[5, "OnJsonApiEvent_chat_v4_presences"]')
                backoff = 2
                while not self.stop.is_set():
                    try:
                        msg = ws.recv()
                    except ws_client.WebSocketTimeoutException:
                        continue
                    if _is_presence_msg(msg):
                        self.changed.set()
            except Exception:
                pass
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            if self.stop.wait(backoff):  # 再接続 (port変更にも追従)
                return
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
