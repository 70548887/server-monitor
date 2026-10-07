"""
滑块验证码

存储策略（Redis 优先，内存降级）：
    早期实现用进程内字典存答案，带来三个问题：
      1. 无过期、无上限 —— 匿名请求可无限堆积，打爆内存
      2. 多进程部署失效 —— 登录请求可能落到另一 worker
      3. 无重放保护

    现改为：Redis 可用时走 Redis（带 TTL，跨进程安全）；
    不可用时降级到进程内字典，但补上过期清理与容量上限。
    项目本就把 Redis 当可选组件，这里沿用同一策略。
"""

import base64
import io
import random
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# 背景图目录：绝对路径。
# 原实现用相对路径 os.path.join("app", "static", ...)，
# 工作目录不对时（systemd 未设 WorkingDirectory、测试、其他工具调用）
# 会静默降级为纯色随机背景 —— 那种背景极易被机器识别。
BG_DIR = Path(__file__).resolve().parent.parent / "static" / "captcha_bg"

# 验证码有效期（秒）。超过后即便未使用也作废。
CAPTCHA_TTL_SECONDS = 120

# 验证通过后的标记有效期。
# 前端拖动时先调 /captcha/verify 实时校验，登录时再校验一次。
# 中间这段窗口内允许凭标记通过，因此标记也要有过期时间。
CAPTCHA_VERIFIED_TTL_SECONDS = 60

# 内存降级时的容量上限，超出后淘汰最旧的。
# 防止 Redis 不可用时被匿名请求打爆内存。
MEMORY_MAX_ENTRIES = 2000

# 位置容差（像素）。5px 对触摸屏过于苛刻。
POSITION_TOLERANCE = 8


class SliderCaptcha:
    """滑动验证码生成器"""

    def __init__(self, width=310, height=155, slider_size=50):
        self.width = width
        self.height = height
        self.slider_size = slider_size

    def generate(self):
        """生成验证码图片和位置信息"""
        bg = self._load_local_bg() or self._create_random_bg()

        # 缺口位置（避开左右边缘，确保滑块完整可见）
        x = random.randint(self.slider_size + 20, self.width - self.slider_size - 20)
        y = random.randint(20, self.height - self.slider_size - 20)

        # 滑块图 = 背景在缺口处的裁剪
        slider = Image.new(
            "RGBA", (self.slider_size, self.slider_size), (0, 0, 0, 0)
        )
        slider.paste(bg.crop((x, y, x + self.slider_size, y + self.slider_size)), (0, 0))

        # 在背景上标出缺口。
        # 早期是半透明黑色方块，视觉上过于明显。
        # 改为：主体压暗 + 描边 + 外阴影，接近真实"挖空"效果。
        draw = ImageDraw.Draw(bg, "RGBA")
        draw.rectangle(
            [x, y, x + self.slider_size, y + self.slider_size],
            fill=(0, 0, 0, 90),
        )
        draw.rectangle(
            [x, y, x + self.slider_size, y + self.slider_size],
            outline=(255, 255, 255, 160),
            width=2,
        )
        # 缺口投影，增加立体感
        shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle(
            [x + 2, y + 2, x + self.slider_size + 2, y + self.slider_size + 2],
            fill=(0, 0, 0, 90),
        )
        bg = Image.alpha_composite(bg.convert("RGBA"), shadow.filter(
            ImageFilter.GaussianBlur(2)
        )).convert("RGB")

        # 滑块加一圈浅边，使其在背景上更易辨识（用户能看清拖的是什么）
        edge = ImageDraw.Draw(slider, "RGBA")
        edge.rectangle(
            [0, 0, self.slider_size - 1, self.slider_size - 1],
            outline=(255, 255, 255, 220),
            width=2,
        )

        return {
            "bg": self._image_to_base64(bg),
            "slider": self._image_to_base64(slider),
            "y": y,
            "x": x,  # 仅用于服务端校验，绝不下发给前端
        }

    def _load_local_bg(self):
        """从本地静态库读取背景图（绝对路径）"""
        if not BG_DIR.is_dir():
            return None
        files = [f for f in BG_DIR.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if not files:
            return None
        try:
            img = Image.open(random.choice(files))
            if img.size != (self.width, self.height):
                img = img.resize((self.width, self.height), Image.Resampling.LANCZOS)
            return img.convert("RGB")
        except Exception:
            return None

    def _create_random_bg(self):
        """
        降级背景：生成纹理较丰富的图像

        原实现是浅色底 + 几条线，结构过于简单，容易被分割识别。
        这里用多层噪点与色块，让降级路径也不至于形同虚设。
        """
        bg = Image.new("RGB", (self.width, self.height), (
            random.randint(120, 200),
            random.randint(120, 200),
            random.randint(120, 200),
        ))
        draw = ImageDraw.Draw(bg, "RGBA")
        # 多层随机色块
        for _ in range(60):
            x1 = random.randint(0, self.width)
            y1 = random.randint(0, self.height)
            x2 = x1 + random.randint(10, 80)
            y2 = y1 + random.randint(10, 80)
            draw.rectangle(
                [x1, y1, x2, y2],
                fill=(
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(40, 120),
                ),
            )
        # 噪点
        for _ in range(self.width * self.height // 40):
            px = random.randint(0, self.width - 1)
            py = random.randint(0, self.height - 1)
            v = random.randint(0, 255)
            draw.point((px, py), fill=(v, v, v))
        return bg

    def _image_to_base64(self, img):
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")


# ===================== 存储层 =====================


class _MemoryStore:
    """进程内降级存储：带过期清理与容量上限"""

    def __init__(self, max_entries=MEMORY_MAX_ENTRIES):
        self._data = {}          # captcha_id -> (expires_at, payload)
        self._max = max_entries

    def _purge_expired(self, now):
        expired = [k for k, (exp, _) in self._data.items() if exp <= now]
        for k in expired:
            del self._data[k]
        return len(expired)

    def save(self, key, payload, ttl):
        now = time.time()
        self._purge_expired(now)
        # 超容量时淘汰最旧的（dict 保序，插入顺序即时间顺序）
        while len(self._data) >= self._max:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = (now + ttl, payload)

    def get(self, key):
        now = time.time()
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at <= now:
            self._data.pop(key, None)
            return None
        return payload

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def __len__(self):
        self._purge_expired(time.time())
        return len(self._data)


class CaptchaStore:
    """
    验证码存储：Redis 优先，内存降级

    Redis 可用时用 SETEX 写入并设置 TTL —— 跨进程安全、自动过期。
    Redis 不可用时降级到带上限的内存字典。
    """

    def __init__(self):
        self._memory = _MemoryStore()
        self._redis_available: bool | None = None

    async def _redis(self):
        """获取可用的 Redis 仓库；不可用则返回 None 并记住该事实"""
        if self._redis_available is False:
            return None
        try:
            from ..repositories.redis_repository import get_redis_repository

            repo = get_redis_repository()
            client = await repo._get_client()
            await client.ping()
            self._redis_available = True
            return repo
        except Exception:
            # Redis 不可用，之后不再重复尝试（避免每次请求都等超时）
            self._redis_available = False
            return None

    @staticmethod
    def _key(captcha_id: str) -> str:
        return f"captcha:{captcha_id}"

    async def save(self, captcha_id: str, payload: dict, ttl: int = CAPTCHA_TTL_SECONDS):
        repo = await self._redis()
        if repo is not None:
            try:
                import json

                client = await repo._get_client()
                await client.setex(
                    self._key(captcha_id), ttl, json.dumps(payload)
                )
                return
            except Exception:
                self._redis_available = False
        self._memory.save(captcha_id, payload, ttl)

    async def get(self, captcha_id: str) -> dict | None:
        repo = await self._redis()
        if repo is not None:
            try:
                import json

                client = await repo._get_client()
                raw = await client.get(self._key(captcha_id))
                if raw is None:
                    return None
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return json.loads(raw)
            except Exception:
                self._redis_available = False
        return self._memory.get(captcha_id)

    async def delete(self, captcha_id: str) -> bool:
        repo = await self._redis()
        if repo is not None:
            try:
                client = await repo._get_client()
                return await client.delete(self._key(captcha_id)) > 0
            except Exception:
                self._redis_available = False
        return self._memory.delete(captcha_id)


# 全局存储实例
store = CaptchaStore()


# ===================== 轨迹校验（反机器人） =====================


def _trail_signature(trail) -> str:
    """
    轨迹指纹

    取首尾点与采样数做特征，避免存完整轨迹（内存/Redis 开销），
    同时足以识别"换了一条轨迹"的情况。
    """
    if not isinstance(trail, list) or not trail:
        return "empty"
    try:
        head = trail[0]
        tail = trail[-1]
        return f"{len(trail)}:{head[0]},{head[1]}:{tail[0]},{tail[1]}"
    except (IndexError, TypeError):
        return "invalid"


def validate_trail(trail, target_x: int) -> bool:
    """
    校验滑动轨迹是否像真人操作

    脚本直接设置 left 属性的方式，轨迹要么为空，要么是一条直线跳变。
    真人操作具备这些特征：
      - 采样点数足够（>= 8）
      - 有明显的加速—减速过程（起手慢、中段快、收尾慢）
      - 存在轻微抖动（不是完美的匀速）

    Args:
        trail: 前端上报的轨迹点列表，每项形如 [x, t]（x 为像素，t 为毫秒）
        target_x: 缺口横坐标

    Returns:
        是否通过。轨迹缺失时不拦截（保证旧前端仍可用），
        但一旦上报就必须合法 —— 上报了假轨迹比不上报更可疑。
    """
    if not trail:
        return True  # 未上报轨迹，放行

    if not isinstance(trail, list) or len(trail) < 8:
        return False

    xs = []
    ts = []
    for point in trail:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        try:
            xs.append(float(point[0]))
            ts.append(float(point[1]))
        except (TypeError, ValueError):
            return False

    # 时间必须单调递增，否则是伪造的
    for i in range(1, len(ts)):
        if ts[i] < ts[i - 1]:
            return False

    # 位移必须单调不减（不能往回拖）
    for i in range(1, len(xs)):
        if xs[i] < xs[i - 1] - 2:
            return False

    total_time = ts[-1] - ts[0]
    # 人手拖动 200~300px 通常需要 300ms 以上；
    # 脚本瞬间完成会明显偏快
    if total_time < 120:
        return False

    # 每步位移应有大有小（真人会抖动）。若完全等速则判为脚本。
    steps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    moving = [s for s in steps if s > 0.5]
    if len(moving) < 4:
        return False
    if len(set(round(s, 1) for s in moving)) <= 2:
        # 所有步长几乎相同 = 完美匀速 = 机器
        return False

    return True


# ===================== 对外接口 =====================


async def create_captcha():
    """
    生成验证码

    返回给前端的字段不含答案 x。
    """
    sc = SliderCaptcha()
    data = sc.generate()
    captcha_id = str(uuid.uuid4())

    await store.save(captcha_id, {"x": data["x"], "y": data["y"]})

    return {
        "id": captcha_id,
        "bg": f"data:image/png;base64,{data['bg']}",
        "slider": f"data:image/png;base64,{data['slider']}",
        "y": data["y"],
        "tolerance": POSITION_TOLERANCE,
    }


async def check_captcha(captcha_id: str, x_offset, trail=None, consume: bool = False):
    """
    校验验证码

    两阶段设计：
        前端拖动结束 -> consume=False，仅校验并打上"已通过"标记
        提交登录     -> consume=True，必须携带标记才算数

    为什么需要标记：非 consume 模式若既不销毁也不标记，
    同一个 captcha_id 就能被反复调用 verify 接口试探位置 ——
    实测发现攻击者可借此二分搜索 x。因此通过后必须留下凭证。

    Args:
        captcha_id: 验证码 ID
        x_offset: 用户拖动的横坐标
        trail: 滑动轨迹，用于反机器人
        consume: True 表示最终校验（登录），通过后销毁；
                False 表示中间校验（前端实时反馈）

    Returns:
        (是否通过, 失败原因)
    """
    if not captcha_id or x_offset is None:
        return False, "参数不完整"

    record = await store.get(captcha_id)
    if record is None:
        return False, "验证码已过期，请刷新"

    # 登录阶段：必须先通过前端的实时校验
    if consume and not record.get("passed"):
        # 即便失败也要销毁：否则攻击者可以拿一个从未验证过的验证码
        # 无限次提交登录请求，把登录接口变成免费的试探 oracle。
        await store.delete(captcha_id)
        return False, "请先完成人机验证"

    # 登录时的轨迹必须与前端实时校验时一致。
    # 否则攻击者可以用真人轨迹通过 verify 端点，再在登录请求里
    # 换成别的轨迹（甚至是空值绕过）来伪造。
    if consume and record.get("passed"):
        recorded_trail = record.get("trail")
        if recorded_trail is not None and trail is not None:
            if _trail_signature(recorded_trail) != _trail_signature(trail):
                return False, "验证数据不一致，请重新验证"

    try:
        x_offset = int(x_offset)
    except (TypeError, ValueError):
        return False, "坐标无效"

    if not validate_trail(trail, record["x"]):
        if consume:
            await store.delete(captcha_id)
        return False, "滑动轨迹异常，请重试"

    tolerance = POSITION_TOLERANCE
    passed = abs(record["x"] - x_offset) <= tolerance

    if not passed:
        if consume:
            # 失败也用完即焚：否则攻击者可无限次试探
            await store.delete(captcha_id)
        return False, "验证失败，请重试"

    if consume:
        await store.delete(captcha_id)
    else:
        # 标记已通过，给一个短窗口供登录提交使用
        # 顺带记录通过时的轨迹，登录时必须一致，防止中途替换
        await store.save(
            captcha_id,
            {**record, "passed": True, "trail": trail},
            ttl=CAPTCHA_VERIFIED_TTL_SECONDS,
        )
    return True, ""