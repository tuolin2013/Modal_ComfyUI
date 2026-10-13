"""
产品素材库：多张实拍图 → 自动分类 → 按详情页每屏的需求分配素材 → 决定每屏的出图方式

用户一次上传多张混杂的照片（手机随手拍、精修包装正反面、原料植株、干品、冲泡/使用中……），
不需要自己指定哪张用在哪屏：
  1. 入库：每张图算本地质量分（清晰度、分辨率、是否白底，不调接口），视觉模型打类型标签（ecom_images.classify_photos）
     用户可在界面上改类型，改过的以用户为准（type_source = "user"），重新识别时不覆盖
  2. 每屏声明需要什么素材（role_needs，按屏的作用文字匹配规则，自定义类目手册也适用）
  3. 分配（assign）：按屏的顺序给每屏选一张最合适的素材，打分 = 类型匹配 + 质量 − 重复使用 − 相邻屏同图
  4. 出图方式：
       photo     实拍直出：照片好、这一屏要的正是证据（茶汤、叶底、原料、证书），程序直接排版，不让 AI 重画
       reference 参考重绘：需要新构图，包装正面锁定外观，内容照片作素材参考
       generate  纯生成：不出现产品的氛围屏（如痛点），文案不能写「实拍」

本文件只放数据、纯函数和素材库的读写，不调用任何接口。
"""

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

LIB_DIR = Path(__file__).resolve().parent / "output" / "ecom" / "product"
MAX_PHOTOS = 20
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# ── 照片类型 ──────────────────────────────────────────────────────────────────
# key: (中文名, 给生图模型的英文说明, 给分类模型的判别说明)
PHOTO_TYPES: dict[str, tuple[str, str, str]] = {
    "package_front": ("包装正面", "the real product packaging, front view",
                      "能看清品牌名 / 产品名的包装正面（精修图或实拍都算）"),
    "package_back": ("包装背面 / 标签", "the back label of the packaging",
                     "配料表、营养成分 / 成分分析保证值、生产许可证、条码所在的那一面"),
    "package_other": ("包装侧面 / 组合 / 礼盒", "the real packaging from another angle or as a set",
                      "包装的侧面、斜角、多件组合、礼盒、开箱"),
    "raw_material": ("原料 / 产地", "the raw material or its origin (plant, field, landscape)",
                     "原料植株、鲜叶、种植地、山景、原料堆，画面里没有成品包装"),
    "product_closeup": ("产品本体特写", "the product itself out of its package (e.g. dry tea leaves, tablets)",
                        "拆包后的内容物：干茶、片剂、颗粒、粉末、膏体等"),
    "in_use": ("使用中 / 冲泡 / 进食", "the product in use (e.g. brewed tea in a cup, a pet eating it)",
               "冲泡好的茶汤、正在冲泡、宠物正在吃、产品正在被使用"),
    "texture_detail": ("质地 / 叶底细节", "a macro detail of the product's texture (e.g. infused leaves)",
                       "叶底、断面、质地、表面白霜等微距细节"),
    "lifestyle": ("生活场景", "an everyday lifestyle scene", "有人或宠物的日常生活画面，产品不是主体"),
    "certificate": ("证书 / 检测报告", "a certificate or test report", "证书、检测报告、许可证、奖状的照片或扫描件"),
    "other": ("其他", "a related photo", "以上都不是"),
}
DEFAULT_TYPE = "other"
PACKAGE_TYPES = ("package_front", "package_other", "package_back")
# 文字很多的素材只能实拍直出：让生图模型照着画会把字画花
PHOTO_ONLY_TYPES = {"package_back", "certificate"}

# 模型或旧数据给出的别名 → 标准类型
_TYPE_ALIASES = {
    **{v[0]: k for k, v in PHOTO_TYPES.items()},
    "front": "package_front", "package": "package_front", "包装": "package_front", "正面": "package_front",
    "back": "package_back", "label": "package_back", "背面": "package_back", "标签": "package_back",
    "side": "package_other", "gift_box": "package_other", "侧面": "package_other", "礼盒": "package_other",
    "plant": "raw_material", "origin": "raw_material", "原料": "raw_material", "产地": "raw_material",
    "dry_product": "product_closeup", "closeup": "product_closeup", "干茶": "product_closeup",
    "brewed": "in_use", "pet_eating": "in_use", "冲泡": "in_use", "茶汤": "in_use",
    "texture": "texture_detail", "macro": "texture_detail", "叶底": "texture_detail",
    "scene": "lifestyle", "场景": "lifestyle", "cert": "certificate", "证书": "certificate",
}

# 旧版 3 个图位的「角度说明」→ 类型（迁移用）
_LABEL_GUESS = (
    (r"背面|配料|标签|成分表", "package_back"), (r"证书|报告|许可", "certificate"),
    (r"侧面|斜|45|礼盒|组合", "package_other"), (r"正面|包装|白底", "package_front"),
    (r"叶底|质地|微距", "texture_detail"), (r"冲泡|茶汤|使用|进食|喂", "in_use"),
    (r"干茶|特写|细节|内容物", "product_closeup"), (r"产地|原料|植株|山", "raw_material"),
    (r"场景|生活", "lifestyle"),
)


def normalize_type(value) -> str:
    s = str(value or "").strip()
    if s in PHOTO_TYPES:
        return s
    return _TYPE_ALIASES.get(s) or _TYPE_ALIASES.get(s.lower()) or DEFAULT_TYPE


def guess_type(text: str) -> str:
    for pattern, key in _LABEL_GUESS:
        if re.search(pattern, str(text or "")):
            return key
    return DEFAULT_TYPE


def type_label(key: str) -> str:
    return PHOTO_TYPES.get(key, PHOTO_TYPES[DEFAULT_TYPE])[0]


def type_choices() -> list[tuple[str, str]]:
    return [(v[0], k) for k, v in PHOTO_TYPES.items()]


def tag_guide() -> str:
    """给分类模型的类型说明"""
    return "\n".join(f"- {k}：{v[0]}。{v[2]}" for k, v in PHOTO_TYPES.items())


def normalize_tags(raw, n: int) -> list[tuple[str, str] | None]:
    """
    分类模型的输出 → 每张图 (类型, 一句描述)；没给到的图为 None。
    接受 {"photos": [{"index": 1, "type": ..., "note": ...}]} 或直接是列表；index 从 1 起，缺了按顺序对应
    """
    items = raw.get("photos") if isinstance(raw, dict) else raw
    out: list[tuple[str, str] | None] = [None] * n
    for k, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index")) - 1
        except (TypeError, ValueError):
            idx = k
        if 0 <= idx < n:
            note = re.sub(r"\s+", " ", str(it.get("note") or "")).strip()[:24]
            out[idx] = (normalize_type(it.get("type")), note)
    return out


# ── 本地质量分（不调接口）────────────────────────────────────────────────────
# 清晰度用边缘密度（FIND_EDGES 后的平均亮度，与留白区检查同一个度量），缩到 512 边长再算。
# 阈值是经验值，样本不多：清晰实拍一般 ≥ 8，明显虚焦 / 糊图 < 4
SHARP_REF = 10.0
RES_REF = 1080          # 短边达到这个像素数算满分
GOOD_QUALITY = 0.45     # 证据屏「实拍直出」的最低质量分


def photo_quality(path: str) -> dict:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = img.size
    g = img.convert("L")
    g.thumbnail((512, 512))
    sharp = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0]
    gw, gh = g.size
    b = max(2, int(min(gw, gh) * 0.04))
    stats = [ImageStat.Stat(g.crop(box)) for box in ((0, 0, gw, b), (0, gh - b, gw, gh),
                                                    (0, 0, b, gh), (gw - b, 0, gw, gh))]
    white = all(s.mean[0] > 238 and s.stddev[0] < 12 for s in stats)
    score = 0.6 * min(1.0, sharp / SHARP_REF) + 0.4 * min(1.0, min(w, h) / RES_REF)
    return {"width": w, "height": h, "sharpness": round(sharp, 1), "white_bg": white, "score": round(score, 2)}


def quality_note(q: dict | None) -> str:
    if not q:
        return ""
    bits = [f"{q.get('width')}×{q.get('height')}", f"质量 {q.get('score', 0):.2f}"]
    if q.get("score", 1) < GOOD_QUALITY:
        bits.append("偏糊或偏小")
    if q.get("white_bg"):
        bits.append("白底")
    return " · ".join(bits)


# ── 素材库读写 ────────────────────────────────────────────────────────────────
def _index_path() -> Path:
    return LIB_DIR / "library.json"


def _with_path(e: dict) -> dict:
    return {**e, "path": str(LIB_DIR / e["file"])}


def _sha1(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _migrate_legacy() -> list[dict]:
    """旧版是 product_1..3 三个图位 + labels.json（角度说明）：图 1 当包装正面，其余按说明猜类型"""
    if not LIB_DIR.exists():
        return []
    try:
        labels = json.loads((LIB_DIR / "labels.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        labels = {}
    lib = []
    for slot in range(1, 10):
        hits = sorted(LIB_DIR.glob(f"product_{slot}.*"))
        if not hits:
            continue
        note = str(labels.get(str(slot)) or "").strip()[:24]
        guess = guess_type(note)
        kind = "package_front" if slot == 1 and guess == DEFAULT_TYPE else guess
        try:
            q = photo_quality(str(hits[0]))
        except OSError:
            continue
        lib.append({"id": f"P{len(lib) + 1:02d}", "file": hits[0].name, "type": kind, "type_source": "legacy",
                    "note": note, "quality": q, "sha1": _sha1(hits[0])})
    if lib:
        save_library(lib)
    return lib


def load_library() -> list[dict]:
    """素材库（按上传顺序），每项带 path；文件已被删的条目跳过"""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
        lib = [e for e in data if isinstance(e, dict) and e.get("id") and e.get("file")]
    except (OSError, json.JSONDecodeError, TypeError):
        lib = _migrate_legacy()
    return [_with_path(e) for e in lib if (LIB_DIR / e["file"]).exists()]


def save_library(lib: list[dict]):
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    clean = [{k: v for k, v in e.items() if k != "path"} for e in lib]
    _index_path().write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")

def add_photos(paths: list[str]) -> tuple[list[dict], list[str]]:
    """复制进素材库并算质量分（类型记为待识别）。返回 (新增条目, 跳过说明)；同一张图重复上传会跳过"""
    lib = load_library()
    seen = {e.get("sha1") for e in lib}
    next_no = max([int(e["id"][1:]) for e in lib if e["id"][1:].isdigit()] or [0]) + 1
    added, skipped = [], []
    for p in paths or []:
        name = Path(p).name
        if Path(p).suffix.lower() not in IMAGE_SUFFIXES:
            skipped.append(f"{name}：不是图片")
            continue
        if len(lib) >= MAX_PHOTOS:
            skipped.append(f"{name}：素材库最多 {MAX_PHOTOS} 张")
            continue
        digest = _sha1(p)
        if digest in seen:
            skipped.append(f"{name}：已经在素材库里")
            continue
        try:
            q = photo_quality(p)
        except OSError as e:
            skipped.append(f"{name}：打不开（{type(e).__name__}）")
            continue
        pid = f"P{next_no:02d}"
        next_no += 1
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        dest = LIB_DIR / f"{pid}{Path(p).suffix.lower()}"
        shutil.copy(p, dest)
        entry = _with_path({"id": pid, "file": dest.name, "type": DEFAULT_TYPE, "type_source": "pending",
                            "note": "", "quality": q, "sha1": digest})
        lib.append(entry)
        seen.add(digest)
        added.append(entry)
    save_library(lib)
    return added, skipped


def find(lib: list[dict], pid: str) -> dict | None:
    return next((e for e in lib if e["id"] == pid), None)


def find_same(lib: list[dict], path: str) -> dict | None:
    """素材库里和这个文件内容相同的照片（重复上传时找回已有的条目）"""
    digest = _sha1(path)
    return next((e for e in lib if e.get("sha1") == digest), None)


def update_photo(pid: str, type_source: str = "user", **fields) -> dict | None:
    """改类型（默认记为用户确认，重新识别时不覆盖）、备注或识别出的标签字段；返回更新后的条目"""
    lib = load_library()
    e = find(lib, pid)
    if not e:
        return None
    if "type" in fields:
        e["type"] = normalize_type(fields["type"])
        e["type_source"] = type_source
    if "note" in fields:
        e["note"] = re.sub(r"\s+", " ", str(fields["note"] or "")).strip()[:24]
    if "label_fields" in fields:
        e["label_fields"] = normalize_label_fields(fields["label_fields"])
    save_library(lib)
    return e


def remove_photo(pid: str) -> bool:
    lib = load_library()
    e = find(lib, pid)
    if not e:
        return False
    Path(e["path"]).unlink(missing_ok=True)
    save_library([x for x in lib if x["id"] != pid])
    return True


def clear_library():
    if LIB_DIR.exists():
        shutil.rmtree(LIB_DIR)


def describe(e: dict) -> str:
    """界面和提示词里用的一句话：P03 使用中 / 冲泡 / 进食（玻璃杯里的茶汤）"""
    note = f"（{e['note']}）" if e.get("note") else ""
    return f"{e['id']} {type_label(e.get('type', DEFAULT_TYPE))}{note}"


def _quality(e: dict) -> float:
    return float((e.get("quality") or {}).get("score", 0.5))


def package_refs(lib: list[dict], limit: int = 3) -> list[dict]:
    """锁定产品外观用的参考图：包装正面优先、其次侧面/组合，同类按质量；
    一张包装图都没有时退回其它照片按质量排（背面 / 证书除外）"""
    order = {"package_front": 0, "package_other": 1}
    pkgs = sorted((e for e in lib if e.get("type") in order), key=lambda e: (order[e["type"]], -_quality(e)))
    if not pkgs:
        pkgs = sorted((e for e in lib if e.get("type") not in PHOTO_ONLY_TYPES), key=lambda e: -_quality(e))
    return pkgs[:limit]


def needs_tagging(lib: list[dict], force: bool = False) -> list[dict]:
    """需要（重新）识别的照片：用户改过类型的永远不动；force 时其余全部重识别"""
    return [e for e in lib if e.get("type_source") != "user" and (force or e.get("type_source") == "pending")]


# ── 包装背面标签：视觉模型读出的字段（需人工核对）────────────────────────────
def normalize_label_fields(value) -> dict[str, str]:
    """{"配料": "...", "净含量": "..."} → 清洗后的 dict；最多 16 项，名 ≤ 10 字、值 ≤ 80 字"""
    out = {}
    for k, v in (value.items() if isinstance(value, dict) else []):
        k = re.sub(r"\s+", "", str(k or ""))[:10]
        v = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:80]
        if k and v:
            out[k] = v
    return dict(list(out.items())[:16])


def label_context(lib: list[dict]) -> str:
    """包装背面识别出的标签信息，拼进策划输入；没有则空字符串"""
    fields = {}
    for e in lib:
        if e.get("type") == "package_back":
            for k, v in (e.get("label_fields") or {}).items():
                fields.setdefault(k, v)
    if not fields:
        return ""
    return ("包装标签（视觉模型从包装背面照片读取，商家尚未核对；规格参数屏优先用这些信息，不要另编）：\n"
            + "\n".join(f"- {k}：{v}" for k, v in fields.items()))

# ── 每屏需要什么素材 ──────────────────────────────────────────────────────────
# 按屏的作用文字匹配（第一条命中的生效），内置、类目覆盖、自定义手册的屏都适用。
#   types：想要的照片类型，越靠前越合适
#   product：画面里是否出现产品（出现时附带包装图锁定外观）
#   photo：照片够好时是否适合「实拍直出」（证据型的屏）
#   shots：适合的拍法，越靠前越合适；相邻屏不重复
#   real：这屏的画面主体就是真实的产品内容（原料、茶汤、叶底、证书……），没有对口照片时不让 AI 凭空画
ROLE_RULES: list[tuple[str, dict]] = [
    (r"白底", dict(types=["package_front"], product=True, photo=False, shots=["hero"])),
    (r"规格|参数|包装清单|配料表", dict(types=["package_back", "package_other", "package_front"], product=True,
                                   photo=True, shots=["flatlay", "tabletop"])),
    (r"资质|证据|检测|认证|证书|品质承诺|许可", dict(types=["certificate", "package_back"], product=True, photo=True,
                                         real=True, shots=["tabletop", "flatlay"])),
    (r"痛点", dict(types=["lifestyle"], product=False, photo=False, shots=["lifestyle", "pov"])),
    (r"产地|原料|种植|采摘|鲜叶|山", dict(types=["raw_material", "product_closeup"], product=False, photo=True,
                                  real=True, shots=["landscape", "macro", "lifestyle"])),
    (r"冲泡|喂养方法|喂法|使用方法|用法|怎么用|步骤|食用方法", dict(types=["in_use", "product_closeup"], product=True,
                                                   photo=True, real=True, shots=["pov", "tabletop", "flatlay"])),
    (r"叶底|质地|适口|细节|特写|做工|白霜", dict(types=["texture_detail", "product_closeup", "in_use"], product=True,
                                      photo=True, real=True, shots=["macro", "side"])),
    (r"成分|配方|材质|工艺|制作", dict(types=["product_closeup", "raw_material", "texture_detail"], product=True,
                                photo=True, real=True, shots=["flatlay", "macro", "tabletop"])),
    (r"品牌故事|传统|故事|传承", dict(types=["raw_material", "lifestyle"], product=False, photo=True,
                               shots=["landscape", "lifestyle"])),
    (r"送礼|礼盒|礼赠", dict(types=["package_other", "package_front"], product=True, photo=False,
                         shots=["tabletop", "lifestyle"])),
    (r"场景|适用人群|适用对象|人群|谁", dict(types=["lifestyle", "in_use"], product=True, photo=False,
                                    shots=["lifestyle", "pov"])),
    (r"对比", dict(types=["product_closeup", "in_use"], product=True, photo=False, shots=["tabletop", "flatlay"])),
    (r"售后|发货|保障|收口", dict(types=["package_other", "package_front"], product=True, photo=False,
                            shots=["tabletop", "hero"])),
    (r"首屏|海报|亮相|点击", dict(types=["package_front", "package_other"], product=True, photo=False,
                            shots=["hero", "tabletop"])),
    (r"价值|量感|划算|券", dict(types=["package_other", "package_front"], product=True, photo=False,
                           shots=["flatlay", "hero"])),
]
DEFAULT_NEED = dict(types=["product_closeup", "in_use", "texture_detail", "package_other"], product=True,
                    photo=False, shots=["tabletop", "pov", "macro", "lifestyle"])

# 拍法：写进生图提示词（英文），也用于相邻屏去重
SHOTS: dict[str, tuple[str, str]] = {
    "hero": ("正面主体", "hero shot, the product centered at eye level, clean and iconic"),
    "tabletop": ("45° 桌面静物", "45-degree tabletop still life with a few real props"),
    "flatlay": ("俯拍平铺", "top-down flat lay, neatly arranged"),
    "macro": ("微距特写", "macro close-up with very shallow depth of field"),
    "side": ("侧光低角度", "low-angle close shot with raking side light that reveals texture"),
    "pov": ("第一人称手持", "first-person POV, a hand naturally holding or using the product"),
    "lifestyle": ("生活场景", "candid lifestyle scene, medium shot, nobody posing for the camera"),
    "landscape": ("环境远景", "wide environmental shot of the place, natural light, documentary feel"),
}

MAX_USES = 3              # 同一张内容照片最多用在几屏（包装图不限，它是锁定外观用的）
REUSE_PENALTY = 4.0
NEIGHBOR_PENALTY = 6.0
MODES = {"photo": "实拍直出", "reference": "参考重绘", "generate": "纯生成（无产品）"}


def role_need(role: str, main: bool = False) -> dict:
    """
    main：主图。主图一律出现产品（搜索结果里没有产品的图没人点）、不实拍直出、缺照片也不拦；
    只能实拍直出的类型（背面 / 证书）对主图没用，这类规则跳过，往下匹配（如「细节/证据图：…特写」按特写处理）
    """
    for pattern, need in ROLE_RULES:
        if main and set(need["types"]) <= PHOTO_ONLY_TYPES:
            continue
        if re.search(pattern, str(role or "")):
            return {**need, "product": True, "photo": False, "real": False} if main else need
    return {**DEFAULT_NEED, "photo": False} if main else DEFAULT_NEED


def _title_specific(role: str) -> bool:
    """屏的标题（「：」前）就点明了要什么素材（如「叶底特写」），比只在说明里提到（「核心卖点：一层白霜」）更专门"""
    head = re.split(r"[：:]", str(role or ""), maxsplit=1)[0]
    return any(re.search(p, head) for p, _ in ROLE_RULES)


def _match(need: dict, e: dict) -> float | None:
    """类型匹配分：首选 10，之后每档 −3；不匹配返回 None"""
    t = e.get("type")
    if t not in need["types"]:
        return None
    return 10.0 - 3.0 * need["types"].index(t)

def assign(roles: list[str], lib: list[dict], allow_photo: bool = True,
           fixed: dict[int, dict] | None = None, main: bool = False) -> list[dict]:
    """
    给每屏分配素材，返回与 roles 等长的列表，每项：
      {"content": 内容照片 id 或 "", "refs": [参考图 id，包装图在前], "mode": photo|reference|generate,
       "missing": 缺的首选照片类型（没有完全对口的照片时）, "source": "auto"}
    贪心：候选越少（需求越专门）的屏越先挑，同样多时标题点明需求的屏先挑；
    分数 = 类型匹配 + 3×质量 − 重复使用 − 相邻屏同图。
    fixed：{屏序号(0 起): 已有分配}，用户手动改过的屏保持不变，并参与重复 / 相邻惩罚
    main：给主图分配（见 role_need），主图不实拍直出
    """
    fixed = fixed or {}
    allow_photo = allow_photo and not main
    needs = [role_need(r, main) for r in roles]
    out: list[dict | None] = [fixed.get(i) for i in range(len(roles))]
    uses = Counter(a["content"] for a in out if a and a.get("content"))
    # 主图不实拍直出，文字多的照片（背面 / 证书）对主图没用
    cands = [[e for e in lib if _match(n, e) is not None and not (main and e.get("type") in PHOTO_ONLY_TYPES)]
             for n in needs]
    order = sorted((i for i in range(len(roles)) if out[i] is None),
                   key=lambda i: (len(cands[i]) or 99, not _title_specific(roles[i]), i))
    pkg = package_refs(lib, 2)
    for i in order:
        need = needs[i]
        neighbors = {out[j]["content"] for j in (i - 1, i + 1) if 0 <= j < len(out) and out[j]}
        best, best_score = None, None
        for e in cands[i]:
            if uses[e["id"]] >= MAX_USES and e.get("type") not in PACKAGE_TYPES:
                continue
            s = _match(need, e) + 3.0 * _quality(e) - REUSE_PENALTY * uses[e["id"]] \
                - (NEIGHBOR_PENALTY if e["id"] in neighbors else 0.0)
            if best_score is None or s > best_score:
                best, best_score = e, s
        out[i] = plan_one(need, best, pkg, allow_photo)
        if best:
            uses[best["id"]] += 1
    return out


def plan_one(need: dict, content: dict | None, pkg: list[dict], allow_photo: bool = True,
             mode: str = "") -> dict:
    """
    一屏的出图方式和参考图。mode 为空时自动决定：
      文字多的照片（背面 / 证书）或证据屏 + 照片够清楚 → 实拍直出
      要出现产品或内容照片是包装 → 参考重绘（包装图锁外观 + 内容照片）
      都不是 → 纯生成
    mode 指定时按指定的来（用户在卡片上改的），条件不满足就退一档
    needs_photo：画面主体是真实产品内容（need.real）却没有对口照片。自动分配时这屏先不出图，等商家补拍；
                 用户手动指定了出图方式就以用户为准
    """
    missing = "" if content and content.get("type") == need["types"][0] else need["types"][0]
    cid = content["id"] if content else ""
    ctype = content.get("type") if content else ""
    good = bool(content) and _quality(content) >= GOOD_QUALITY
    auto_photo = ctype in PHOTO_ONLY_TYPES or (need["photo"] and good and ctype in need["types"][:2])
    if content and allow_photo and (mode == "photo" or (not mode and auto_photo)):
        return {"content": cid, "refs": [cid], "mode": "photo", "missing": missing, "needs_photo": False,
                "source": "auto"}
    if ctype in PHOTO_ONLY_TYPES:     # 不实拍直出时，文字多的照片不拿去重绘（会把字画花）
        cid, ctype = "", ""
    needs_photo = bool(need.get("real")) and not cid and not mode
    product = need["product"] if mode != "generate" else False
    refs = [e["id"] for e in pkg] if (product or ctype in PACKAGE_TYPES) and mode != "generate" else []
    if cid and cid not in refs and mode != "generate":
        refs.append(cid)
    if not refs:
        return {"content": "", "refs": [], "mode": "generate", "missing": missing, "needs_photo": needs_photo,
                "source": "auto"}
    return {"content": cid, "refs": refs, "mode": "reference", "missing": missing, "needs_photo": needs_photo,
            "source": "auto"}


def plan_shots(roles: list[str], assets: list[dict] | None = None) -> list[str]:
    """
    每屏的拍法：在这一屏偏好的前两种里挑整套用得少的，并跳过上一屏的拍法。
    实拍直出的屏记为 photo（画面就是照片本身）
    """
    shots, used = [], Counter()
    for i, role in enumerate(roles):
        if assets and i < len(assets) and assets[i] and assets[i].get("mode") == "photo":
            shots.append("photo")
            continue
        prev = shots[-1] if shots else ""
        prefs = [s for s in role_need(role)["shots"] if s != prev] or [s for s in SHOTS if s != prev]
        pick = min(prefs[:2], key=lambda s: (used[s], prefs.index(s)))
        shots.append(pick)
        used[pick] += 1
    return shots


def shot_label(key: str) -> str:
    return "实拍照片" if key == "photo" else SHOTS.get(key, ("", ""))[0]


def shot_prompt(key: str) -> str:
    return SHOTS.get(key, ("", ""))[1]


def shot_choices() -> list[tuple[str, str]]:
    return [(v[0], k) for k, v in SHOTS.items()]


def check_variety(details: list[dict]) -> list[str]:
    """详情页雷同检查：相邻两屏同一张内容照片 / 同一拍法 / 同一种信息图版式；整套拍法太单一"""
    issues = []
    for a, b in zip(details, details[1:]):
        ca, cb = (a.get("assets") or {}).get("content"), (b.get("assets") or {}).get("content")
        if ca and ca == cb:
            issues.append(f"{a['id']}/{b['id']} 相邻两屏用了同一张照片 {ca}")
        if a.get("shot") and a.get("shot") == b.get("shot") and a["shot"] != "photo":
            issues.append(f"{a['id']}/{b['id']} 相邻两屏都是「{shot_label(a['shot'])}」")
        if a.get("layout") not in (None, "", "standard") and a.get("layout") == b.get("layout"):
            issues.append(f"{a['id']}/{b['id']} 相邻两屏都是「{a['layout']}」信息图")
    kinds = {d.get("shot") for d in details if d.get("shot")}
    if len(details) >= 8 and len(kinds) < 4:
        issues.append(f"详情页只用了 {len(kinds)} 种拍法，画面容易雷同")
    return issues


def signature(lib: list[dict]) -> str:
    """素材库指纹（照片 id + 类型）：和策划时记下的不同，说明补拍 / 删图 / 改过类型，生成前要重新分配素材"""
    return "|".join(f"{e['id']}:{e.get('type', DEFAULT_TYPE)}" for e in lib)


def missing_types(assets: list[dict]) -> list[str]:
    """整套里缺的照片类型（按缺的屏数排序），提示用户补拍"""
    c = Counter(a["missing"] for a in assets if a and a.get("missing"))
    return [f"{type_label(t)}（{n} 屏）" for t, n in c.most_common()]


def asset_note(a: dict | None, lib: list[dict]) -> str:
    """卡片上显示：🖼 实拍直出 · P03 使用中（茶汤）  或  🎨 参考重绘 · 包装 P01 + 内容 P05"""
    if not a:
        return ""
    mode = MODES.get(a.get("mode"), "")
    if a.get("mode") == "generate":
        text = f"🎨 {mode}"
    else:
        content = find(lib, a.get("content") or "")
        refs = [r for r in a.get("refs") or [] if r != a.get("content")]
        bits = ([f"包装 {'、'.join(refs)}"] if refs else []) + ([f"内容 {describe(content)}"] if content else [])
        text = f"{'🖼' if a.get('mode') == 'photo' else '🎨'} {mode} · " + " + ".join(bits)
    if a.get("needs_photo"):
        text += (f"　⛔ 缺「{type_label(a['missing'])}」实拍，补拍前不出图，免得 AI 凭空画；"
                 "不补拍就在下面手动选出图方式")
    elif a.get("missing"):
        text += f"　（缺「{type_label(a['missing'])}」照片，补拍后点生成会自动换上）"
    return text
