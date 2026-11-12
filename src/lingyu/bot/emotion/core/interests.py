"""兴趣：她对哪些话题愿意开口，以及这份愿意是怎么长出来的。

为什么这是一个独立模块而不是又一条情绪规则：情绪回答「我现在什么感受」，
兴趣回答「我一直对什么有兴趣」。前者是几小时的事，后者是几个月的事——
把两者塞进同一个数值，就会出现「今天心情好所以对历史感兴趣」这种荒谬结论。

## 三层，对应项目原有的三层写入权限

| 层 | 字段 | 谁能改 | 性质 |
|---|---|---|---|
| 根源记忆 | ``self.interests`` | 没人（身份文件 seed） | 她生来就偏向什么，不可变 |
| 演化值 | ``interest.values.*`` | 兴趣 Agent，每轮小步 | 经历塑形后的实际兴趣 |
| 本轮判定 | ``interest.topic`` / ``.attention`` | 兴趣 Agent | 这一句话值不值得接 |

这与 ``needs.*.priority``（不可写）与 ``needs.*.salience``（可写）是同一种分工：
长期偏好由身份定，短期强度由经历推，两者永远不混成一个数。

## 兴趣怎么决定「何时说话」

不是「感兴趣就一定说」，而是兴趣调制**注意度 attention**：

- 注意度高 → 调节策略更容易走到 ``share``（想接话），表达上限更高，
  宿主侧的节奏也更快（``pacing`` 里第一条几乎立刻发）；
- 注意度低 → 策略收到 ``hold``，上限压低，甚至可以沉默；
- 完全不切题（没有任何类目命中）→ 中性，**不惩罚**。否则「你好」「在吗」
  这类没有话题的消息会永远得不到回复。

## 兴趣值怎么长

``target = 根源 × (1 − 可塑性) + 这次聊得怎么样 × 可塑性``，然后朝 target 走一小步。
两个方向都受约束：

- 向上：聊得开心才会涨，不是「聊得多就喜欢」——否则复读一个话题就能刷满；
- 向下：聊得难受会掉，但掉不到 0，且每次都会朝根源拉回一点点。

所以「她喜欢工程机械」是根源给的底色，「最近越来越爱聊」是经历加的，而
「被这个话题伤到过」会让她暂时躲开，但不会变成另一个人。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import clamp

#: 兴趣值的上下限。留出余量而不是 0/1：0 意味着「永远不提」，1 意味着
#: 「没有上限」，两者都会让后续的经历无法再改变它。
FLOOR = 0.02
CEIL = 0.98

#: 每轮朝目标值走的比例。0.10 意味着一次强烈的好体验大约要走十几轮才走到位——
#: 这是刻意的：一轮对话就改变长期偏好，那不是成长，是善变。
LEARN_RATE = 0.10

#: 没被谈到时朝根源回归的比例。很小，只用来防止「一次意外」永久改写底色。
PULL_BACK = 0.02

#: 「聊腻了」的衰减：最近几轮里同一类目出现过 n 次，注意度乘 ``1 - 0.07n``，
#: 但**有下限** ``SATIETY_FLOOR``。
#:
#: 下限是必须的：连续十轮同一个话题确实会腻，但「腻」不等于「不想说话」。
#: 没有下限的话，注意度会被压到 5%，于是「越聊越有兴趣」和「越来越不想接」
#: 会同时发生——自相矛盾的两种结论，用户看到的就是「它明明喜欢却装死」。
SATIETY_STEP = 0.07
SATIETY_FLOOR = 0.58
SATIETY_WINDOW = 6


@dataclass(frozen=True)
class Category:
    """一个兴趣类目。关键词是确定性的——兴趣不该由模型「感觉」出来。"""

    id: str
    name: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


#: 类目表。顺序即界面上的展示顺序；关键词按「越长越具体」排列，
#: 长词天然权重更高（见 ``_weight``），所以「发动机」压过「机器」。
CATEGORIES: tuple[Category, ...] = (
    Category("software", "软件", (
        "代码", "编程", "程序", "算法", "数据库", "服务器", "架构", "重构",
        "调试", "编译", "开源", "仓库", "接口", "部署", "脚本", "前端",
        "后端", "框架", "依赖", "报错", "日志", "单元测试", "版本控制",
        "python", "javascript", "typescript", "rust", "golang", "java",
        "git", "linux", "docker", "api", "sql", "bug", "软件", "程序员",
    )),
    Category("art", "艺术", (
        "艺术", "音乐", "绘画", "画画", "油画", "水彩", "素描", "摄影",
        "小说", "诗歌", "散文", "文学", "设计", "展览", "美术馆",
        "乐器", "钢琴", "吉他", "小提琴", "作曲", "旋律", "和声", "配色",
        "构图", "策展", "戏剧", "舞台", "雕塑", "陶艺", "书法",
    )),
    Category("engineering", "工程机械", (
        "工程", "机械", "发动机", "齿轮", "轴承", "机床", "车床", "液压",
        "气动", "焊接", "铸造", "锻造", "公差", "装配", "传动", "扭矩",
        "马力", "电路", "电机", "传感器", "液压泵", "伺服", "机器人",
        "硬件", "拆机", "零件", "图纸", "结构件", "材料", "汽车", "摩托",
        "变速箱", "底盘", "悬挂", "3d打印", "数控", "车铣", "量具",
    )),
    Category("history", "历史", (
        "历史", "朝代", "考古", "文物", "古代", "史料", "编年", "年表",
        "王朝", "帝国", "战争", "战役", "革命", "条约", "先秦", "汉代",
        "唐代", "宋代", "明代", "清代", "民国", "罗马", "中世纪", "文艺复兴",
        "断代", "出土", "碑刻", "族谱", "方志",
    )),
    Category("science", "科学", (
        "科学", "物理", "化学", "生物", "数学", "天文", "地质", "实验",
        "理论", "公式", "定理", "推导", "量子", "相对论", "进化", "细胞",
        "基因", "元素", "分子", "熵", "引力", "光谱", "望远镜", "显微",
    )),
    Category("work", "工作", (
        "工作", "上班", "加班", "老板", "领导", "同事", "项目", "面试",
        "简历", "工资", "薪水", "跳槽", "离职", "汇报", "会议", "需求",
        "排期", "考核", "绩效", "裁员", "实习", "转正", "打工", "甲方",
    )),
    Category("life", "生活", (
        "吃饭", "做饭", "菜", "咖啡", "茶", "旅行", "旅游", "天气", "睡觉",
        "失眠", "搬家", "房租", "地铁", "通勤", "宠物", "猫", "狗", "健身",
        "跑步", "散步", "超市", "快递", "周末", "家里", "房间",
    )),
    Category("media", "影视动漫", (
        "电影", "电视剧", "剧集", "综艺", "动漫", "动画", "番剧", "追番",
        "漫画", "纪录片", "up主", "直播", "短视频", "剪辑", "弹幕", "同人",
    )),
    Category("game", "游戏", (
        "游戏", "打游戏", "通关", "存档", "联机", "开黑", "电竞", "副本",
        "抽卡", "氪金", "mod", "主机", "手柄", "steam", "switch", "ps5",
        "我的世界", "魔兽", "塞尔达", "原神",
    )),
    Category("emotion", "情绪关系", (
        "难过", "开心", "焦虑", "委屈", "生气", "烦", "累", "孤独", "压力",
        "关系", "喜欢", "讨厌", "想念", "吵架", "和解", "分手", "朋友",
        "家人", "父母", "陪伴", "在意", "安全感",
    )),
)

CATEGORY_IDS: tuple[str, ...] = tuple(cat.id for cat in CATEGORIES)
CATEGORY_NAMES: dict[str, str] = {cat.id: cat.name for cat in CATEGORIES}

#: 没有任何身份文件时的根源倾向。刻意做得平：没有身份就没有偏好，
#: 而不是替它编一个性格。身份文件会覆盖这里。
DEFAULT_ROOTS: dict[str, float] = {cat.id: 0.40 for cat in CATEGORIES}

#: 默认可塑性：经历最多能把兴趣推离根源 35%。
DEFAULT_PLASTICITY = 0.35

#: 命中强度低于这个值就算「没聊到具体话题」——「嗯」「好」不该被归类。
#: 标定：一个二字具体词（「齿轮」）约 0.25，三字词（「发动机」）约 0.38。
MIN_MATCH = 0.22


def roots_of(identity) -> dict[str, float]:
    """从身份文件取根源兴趣；缺失的类目回落到 ``DEFAULT_ROOTS``。"""
    roots = dict(DEFAULT_ROOTS)
    if identity is None:
        return roots
    raw = getattr(identity, "interest_roots", None) or {}
    for cat_id, value in raw.items():
        if cat_id in roots:
            try:
                roots[cat_id] = clamp(float(value), FLOOR, CEIL)
            except (TypeError, ValueError):
                continue
    return roots


def plasticity_of(identity) -> float:
    if identity is None:
        return DEFAULT_PLASTICITY
    try:
        value = float(getattr(identity, "interest_plasticity", DEFAULT_PLASTICITY))
    except (TypeError, ValueError):
        return DEFAULT_PLASTICITY
    return clamp(value, 0.0, 0.8)


def detect(text: str) -> list[tuple[str, float]]:
    """识别一段话里的兴趣类目，返回 ``[(类目, 命中强度 0~1)]``，强的在前。

    确定性实现，不调模型：兴趣是长期状态，读错一次就会留下痕迹，
    这种地方不适合让模型自由发挥。命中强度按关键词长度加权——
    「齿轮」比「机器」更能说明在聊机械。
    """
    body = str(text or "").lower()
    if not body:
        return []
    scores: list[tuple[str, float]] = []
    for cat in CATEGORIES:
        weight = 0.0
        for keyword in cat.keywords:
            if keyword in body:
                weight += _weight(keyword)
        if weight <= 0:
            continue
        scores.append((cat.id, weight))
    if not scores:
        return []
    scores.sort(key=lambda item: (-item[1], item[0]))
    # 归一化：一个二字具体词约 0.25，两个词约 0.5，三个以上才接近 1。
    return [(cat_id, round(clamp(weight / 2.2, 0.0, 1.0), 3))
            for cat_id, weight in scores]


def satiety(recent: list[str], cat_id: str) -> float:
    """最近几轮里这个类目被谈过的次数 → 「聊腻了」系数（0.58~1）。

    没有这一项，兴趣只会单调上升：连聊二十轮机械，它就会变成只会聊机械。
    但有下限：腻只是「没那么兴奋」，不是「不想说话」。
    """
    count = sum(1 for item in (recent or [])[-SATIETY_WINDOW:] if item == cat_id)
    return round(clamp(1.0 - SATIETY_STEP * count, SATIETY_FLOOR, 1.0), 3)


def attention_of(value: float, match: float, *, satiety_factor: float = 1.0) -> float:
    """兴趣值 + 命中强度 → 本轮注意度。

    注意度不是兴趣本身：一个很爱聊历史的人，在别人说「今天好累」时也不该
    兴致勃勃。所以它乘上「这句话有多切题」，再被「最近聊腻了」压一压。
    """
    base = clamp(value, 0.0, 1.0) * (0.45 + 0.55 * clamp(match, 0.0, 1.0))
    return round(clamp(base * clamp(satiety_factor, 0.0, 1.0), 0.0, 1.0), 3)


def evolve(root: float, current: float, valence: float,
           *, plasticity: float = DEFAULT_PLASTICITY,
           learn_rate: float = LEARN_RATE) -> float:
    """一次经历之后的兴趣值。

    ``valence`` 是这次聊这个话题时的情绪效价（-1~1）。目标值是
    ``根源 ± 可塑性/2``——**不是**把「聊得开心」直接加在当前值上：

    - 开心时目标 = 根源 + 0.5×可塑性，难过时 = 根源 − 0.5×可塑性，
      所以无论多开心，最终也只是「比底色更喜欢一点」，不会变成另一个人；
    - 心情平平时（valence=0）目标就是根源，于是它自然往回走——
      「聊了但没什么感觉」不该改变偏好，只该让偏好回到本来的位置。

    单次经历的步长是 ``learn_rate``，所以一次强烈体验只走十分之一：
    这是刻意的，一轮对话就改写长期偏好不是成长，是善变。
    """
    target = (clamp(root, 0.0, 1.0)
              + plasticity * 0.5 * clamp(valence, -1.0, 1.0))
    return round(clamp(current + learn_rate * (target - current), FLOOR, CEIL), 4)


def pull_toward_root(root: float, current: float) -> float:
    """没被谈到的类目：朝根源极小幅回归。"""
    return round(clamp(current + PULL_BACK * (root - current), FLOOR, CEIL), 4)


def top(values: dict[str, float], limit: int = 3) -> list[dict]:
    """兴趣排行，供界面展示。"""
    items = []
    for cat_id, value in (values or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        items.append({"id": cat_id, "name": CATEGORY_NAMES.get(cat_id, cat_id),
                      "value": round(number, 3)})
    items.sort(key=lambda item: -item["value"])
    return items[:limit]


def _weight(keyword: str) -> float:
    """关键词权重：越长越具体。单字词几乎不算数，避免「剧」命中一切。"""
    length = len(keyword)
    if length <= 1:
        return 0.2
    return 0.55 + 0.28 * (length - 2)
