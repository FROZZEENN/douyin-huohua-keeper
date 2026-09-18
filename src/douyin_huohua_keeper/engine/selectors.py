"""页面选择器集中管理。

抖音前端改版很频繁，这个模块存在的唯一目的是**让改版只需要改一个文件**。

每个关键元素都给了多个候选选择器，按顺序尝试。这样即使主选择器失效，
还能靠备用选择器撑一段时间，而不是直接崩掉。

命名约定：
- ``*_MARKERS``   判定某类页面的存在性标志（只要有一个命中即可）
- ``*_CANDIDATES`` 同一元素的多套选择器（按优先级从高到低）

排查选择器失效的流程：
1. 用 ``--headed`` 模式跑一次，亲眼看看页面渲染了什么
2. 或者设 ``HUOHUA_CAPTURE_ON_ERROR=true``，失败时的截图在
   ``data/runs/<run_id>/`` 里
3. 在浏览器 F12 里找到新结构，加到候选列表的最前面
"""

from __future__ import annotations

# =============================================================================
# 会话页 / 登录页判定
# =============================================================================

# 会话页正常加载的标志。命中任意一个就认为「登录态可用且页面正常」。
#
# ⚠️ 实测教训（2026-09）：抖音的 class 绝大多数是**每次构建都会变的哈希名**
#    （形如 ``RhjdbXj8``），所以 ``[class*="conversationList"]`` 这类
#    类名匹配在真实页面上**一个都不命中**。
#
#    结果就是：明明已经正常进入会话页（左侧一列好友会话清清楚楚），
#    这个判定却报 False，让上层以为「页面不对」。
#
#    所以这里优先用**语义特征**（placeholder 文本、contenteditable、
#    data-e2e），哈希类名只作为兜底保留。
CHAT_PAGE_MARKERS: tuple[str, ...] = (
    # --- 最强信号：会话行已经渲染出来了 ---
    #
    # 必须排在最前面。实测教训：搜索框出现得**远早于**会话列表渲染完，
    # 如果拿搜索框当"页面就绪"的判据，会立刻通过，然后
    # read_conversation_list 读到一份半成品列表（名字还没填上），
    # 表现为「联系人明明在列表里却报找不到」。实测踩过。
    '[data-e2e="conversation-item"]',
    # --- 次强：搜索框（在，但列表可能还没好）---
    'input[placeholder*="搜索"]',
    # --- 输入框（只有在已打开会话时才存在）---
    'div[contenteditable="true"]',
    'textarea[placeholder]',
    # --- 结构化标识（部分版本有）---
    '[data-e2e="chat-list"]',
    # --- 哈希类名兜底（大概率不命中，留着以防旧版）---
    '[class*="conversationList"]',
    '[class*="conversation-list"]',
    '[class*="chatList"]',
    '[class*="sessionList"]',
)

# 未登录 / 被踢下线的标志。命中任意一个就认为「登录态失效」
LOGIN_PAGE_MARKERS: tuple[str, ...] = (
    '[data-e2e="login-modal"]',
    '[class*="login-modal"]',
    '[class*="loginModal"]',
    '[class*="login-panel"]',
    '[class*="qrcode"]',
    '[class*="qr-code"]',
    'img[alt*="二维码"]',
    # 文案兜底：这几个字出现在未登录页上很典型
    'text="扫码登录"',
    'text="登录后查看更多"',
    'button:has-text("登录")',
)


# =============================================================================
# 会话列表
# =============================================================================

# 会话列表容器
CONVERSATION_LIST: tuple[str, ...] = (
    '[data-e2e="chat-list"]',
    '[class*="conversationList"]',
    '[class*="conversation-list"]',
    '[class*="chatList"]',
    '[class*="sessionList"]',
    '[class*="listContainer"]',
)

# 单个会话行。用的是「点进去能打开某个好友会话」的那个可点击元素
CONVERSATION_ITEM: tuple[str, ...] = (
    '[data-e2e="conversation-item"]',
    '[class*="conversation-item"]',
    '[class*="conversationItem"]',
    '[class*="sessionItem"]',
    '[class*="chat-item"]',
    '[class*="chatItem"]',
)

# 会话行里显示名字的元素（相对于行内查找）
#
# ⚠️ 实测（抖音 2026-09）真实类名是 ``conversationConversationItemtitle``，
#    对应的元素就是纯名字（例如 ``阿明``）。
#    它比泛用的 ``[class*="title"]`` 精准得多 —— 后者在每一行里会命中 2 个
#    元素（外层容器含「名字+火花天数+时间」，内层才是纯名字）。
#    放在最前面可以少走弯路；``_extract_name`` 还额外做了「取最短」的保护。
CONVERSATION_NAME: tuple[str, ...] = (
    '[class*="ConversationItemtitle"]',
    '[class*="ConversationItem"] [class*="title"]',
    '[data-e2e="conversation-name"]',
    '[class*="conversation-name"]',
    '[class*="conversationName"]',
    '[class*="nickname"]',
    '[class*="name"]',
    '[class*="title"]',
)

# 会话行里的火花状态（火焰图标 + 天数）
#
# ⚠️ 实测（抖音 2026-09）结构是：
#     <div class="commonStreakstreakContainer">
#       <img class="commonStreakicon">            ← 火焰图标
#       <div class="commonStreaknormalText">849</div>
#     </div>
# 文本可能是纯天数（"849"），也可能是「重燃中 2/3」这种恢复态
# （重燃态的类名是 commonStreaklabelText / commonStreakbackGround）。
# 取容器的 inner_text 即可同时覆盖两种。
STREAK_CANDIDATES: tuple[str, ...] = (
    '[class*="Streak"]',
    '[class*="streak"]',
)

# 会话行里的头像图
#
# ⚠️ 实测结构：头像在 <span class="semi-avatar ..."><img></span> 里，
# 外层容器类名含 IMAvatar。注意行里还有别的 img（火花图标），
# 所以选择器必须先精确匹配头像容器，最后才兜底到所有 img。
AVATAR_CANDIDATES: tuple[str, ...] = (
    '[class*="IMAvataravatarContainer"] img',
    '[class*="IMAvatar"] img',
    # 群头像：群会话用的容器类名可能和单聊不同，单独给候选（放在裸 img 之前）
    '[class*="GroupAvatar"] img',
    '[class*="groupAvatar"] img',
    '[class*="group-avatar"] img',
    '[class*="avatar"] img',
    'span[class*="semi-avatar"] img',
    'img',
)

# 群聊标识：用来判断一行会话是不是群
#
# 抖音的群会话一般会在名字后带「（成员数）」后缀，或者有专门的群头像 / 群标识容器。
# 命中任意一个即认为是群。**宁可漏判也不要错判** —— 把单聊标成「群聊」会让人困惑。
GROUP_MARKERS: tuple[str, ...] = (
    '[class*="GroupAvatar"]',
    '[class*="groupAvatar"]',
    '[class*="group-avatar"]',
    '[class*="GroupIcon"]',
    '[class*="groupIcon"]',
    '[class*="groupTag"]',
    '[class*="GroupTag"]',
)

# 群聊的**文本**信号：会话行文本里出现这些字样，基本可以断定是群。
#
# 实测线索（手机端）：群会话的预览区会出现「1人已读」「[有人@我]」这类
# 单聊不会出现的字样；群名也常被渲染成「张三, 李四, 王五…」这种多人并列。
GROUP_TEXT_MARKERS: tuple[str, ...] = (
    "人已读",
    "有人@",
    "群公告",
    "群聊",
)

# 会话行里的最后一条消息预览（用来做发送确认）
#
# ⚠️ 实测（抖音 2026-09）真实元素是
#     <pre class="ConversationItemHinttextBox">分享[视频]</pre>
# 发送成功后这个位置会变成刚发出的内容，是确认送达最直接的证据。
CONVERSATION_PREVIEW: tuple[str, ...] = (
    '[class*="ConversationItemHint"]',
    '[class*="HinttextBox"]',
    '[data-e2e="conversation-preview"]',
    '[class*="conversation-preview"]',
    '[class*="lastMessage"]',
    '[class*="last-message"]',
    '[class*="preview"]',
    '[class*="content"]',
    '[class*="desc"]',
)

# 会话列表里「消息已读/未读」之类的小红点，可以用来确认消息真的到达了会话列表


# =============================================================================
# 会话详情 / 消息区
# =============================================================================

# 当前正在聊天的对象名字（会话头部）
#
# ⚠️ 实测（抖音 2026-09）：真实的头部名字元素是
#     <div class="RightPanelHeadertitle">阿乐</div>
# 注意它的外层 <div class="RightPanelHeaderinfoContainer"> 里**也有**同样的文字，
# 取错层会把别人的名字读成一整块（或读到容器里的其他内容）。
# 所以优先用最内层的那个类名。
CHAT_HEADER_NAME: tuple[str, ...] = (
    # --- 实测命中 ---
    '[class*="RightPanelHeadertitle"]',
    '[class*="RightPanelHeader"] [class*="title"]',
    # --- 结构化标识（部分版本有）---
    '[data-e2e="chat-header-name"]',
    # --- 旧版 / 通用兜底 ---
    '[class*="chat-header"] [class*="name"]',
    '[class*="chatHeader"] [class*="name"]',
    '[class*="conversation-header"] [class*="name"]',
    '[class*="sessionHeader"] [class*="name"]',
    '[class*="header"] [class*="nickname"]',
)

# 消息气泡列表容器
#
# ⚠️ 实测（2026-09-17，从真实会话页 dump 出来的）：真正的容器 class 是
#    ``messageMessageListlist`` —— 注意里面是 **MessageList**（大写 M、大写 L）。
#    以前的候选全是 ``messageList``（小写 m），**一个都匹配不上** ——
#    于是「数消息条数」「在消息区里找失败标记」这两条判据长期静默失效。
#    加 ``[class*="MessageList"]`` 之后才真正命中。
MESSAGE_LIST: tuple[str, ...] = (
    '[class*="MessageList"]',
    '[data-e2e="message-list"]',
    '[class*="message-list"]',
    '[class*="messageList"]',
    '[class*="chat-content"]',
    '[class*="chatContent"]',
    '[class*="messages"]',
)

# 单条消息气泡。
#
# 用途：给发送确认提供一条**与内容无关**的信号 —— 「会话里的消息条数变多了」。
# 这解决一个真实事故：消息内容恒为「1」时，会话列表预览本来就已经是「1」
# （昨天发的那条），于是「预览 == 期望内容」永远成立 —— 哪怕这次什么都没发，
# 也会被判成成功。
#
# ⚠️ 这些候选**未经真机确认**，所以使用方式刻意设计成「只比较发送前/后」，
#    而不是判断绝对值：选择器不完美时最多是「拿不到这条信号」，
#    绝不会因此误报成功。
#
# ⚠️ 不要加入 ``[class*="messageMsg"]`` 这类过宽的候选 —— 它会连带匹配到
#    输入区（发送按钮的类名就是 ``messageMsgInputpublishBtn``），
#    而发送按钮会随输入框有没有内容出现/消失，导致条数在「没发消息」时也变化，
#    反而制造假阳性。
MESSAGE_ITEM_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="message-item"]',
    '[class*="MessageItem"]',
    '[class*="message-item"]',
    '[class*="messageItem"]',
    '[class*="MessageBubble"]',
    '[class*="messageBubble"]',
    '[class*="chat-message"]',
    '[class*="chatMessage"]',
    '[data-e2e="message-list"] > *',
)

# 「自己发的」消息的标志（相对消息行）

# 消息发送失败的重试标记
# 消息发送失败的标记。
#
# ⚠️ 这里**只放精确的「发送失败」标记**。
# 曾经收尾处有 ``[class*="failed"]`` 和 ``[class*="error"]`` 两个兜底 ——
# 它们是**全页匹配**，页面上任何一个类名带 error/failed 的元素（广告位、
# 加载失败的图片、抖音自己的内部组件）都会命中，于是把一次**成功**的发送
# 判成失败 → 上层重试 → **重复发送**。这是本工具最不能出的错，宁可漏判：
# 漏判的后果只是「未能确认」（不重试、报不确定），而误判的后果是多发一条。
MESSAGE_FAILED: tuple[str, ...] = (
    '[data-e2e="send-failed"]',
    '[class*="send-failed"]',
    '[class*="sendFailed"]',
    '[class*="MessageFailed"]',
    '[class*="sendMsgFailed"]',
    '[class*="resendBtn"]',
)


# =============================================================================
# 输入区
# =============================================================================

# 消息输入框。抖音用的是 contenteditable div，不是 textarea
COMPOSER_INPUT = 'div[contenteditable="true"]'

COMPOSER_INPUT_CANDIDATES: tuple[str, ...] = (
    'div[contenteditable="true"][data-e2e*="input"]',
    'div[contenteditable="true"][class*="editor"]',
    'div[contenteditable="true"][class*="input"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="消息"]',
    'textarea[placeholder*="输入"]',
)

# 发送按钮
#
# ⚠️ 实测（抖音 2026-09）：真正的发送按钮是一个 **SVG 图标**，不是 <button>，
#    它带的类名里有一个**抖音自己留的 e2e 测试类**：
#        <svg class="messageMsgInputpublishBtn messageMsgInputpublishRedBtn e2e-send-msg-btn">
#    ``.e2e-send-msg-btn`` 是最稳的选择器 —— 它不是构建哈希，是专门给测试用的。
#
#    另外重要：这个按钮**只在输入框有内容时才出现/可用**，所以必须
#    「先输入文字，再找按钮」。
SEND_BUTTON_CANDIDATES: tuple[str, ...] = (
    # --- 实测最稳：抖音自带的 e2e 测试类 ---
    '.e2e-send-msg-btn',
    '.messageMsgInputpublishRedBtn',
    '[class*="publishBtn"]',
    '[class*="publishRedBtn"]',
    # --- 结构化 / 语义兜底 ---
    '[data-e2e="send-button"]',
    'button:has-text("发送")',
    '[class*="send-btn"]',
    '[class*="sendButton"]',
    'button[class*="send"]',
    'div[role="button"][class*="send"]',
)

# 表情按钮（打开表情面板）
EMOJI_BUTTON_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="emoji-button"]',
    '[class*="emoji"]',
    '[class*="emoticon"]',
    '[class*="expression"]',
    'button[aria-label*="表情"]',
    '[title*="表情"]',
)

# 表情面板容器
EMOJI_PANEL_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="emoji-panel"]',
    '[class*="emoji-panel"]',
    '[class*="emojiPanel"]',
    '[class*="emoticon-panel"]',
    '[class*="expression-panel"]',
)

# 单个表情项
EMOJI_ITEM_CANDIDATES: tuple[str, ...] = (
    '[class*="emoji-item"]',
    '[class*="emojiItem"]',
    '[class*="emoticon-item"]',
    '[class*="emoji"] img',
    '[class*="emoji"] span',
)

# 图片上传入口
IMAGE_UPLOAD_CANDIDATES: tuple[str, ...] = (
    'input[type="file"][accept*="image"]',
    'input[type="file"]',
)

IMAGE_BUTTON_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="image-button"]',
    '[class*="upload"]',
    '[class*="image-btn"]',
    'button[aria-label*="图片"]',
    '[title*="图片"]',
)

# 搜索框（会话定位的备用通道）
SEARCH_INPUT_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="search-input"]',
    'input[placeholder*="搜索"]',
    'input[placeholder*="查找"]',
    '[class*="search"] input',
    '[class*="searchInput"]',
)

SEARCH_RESULT_ITEM_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="search-result-item"]',
    '[class*="search-result"]',
    '[class*="searchResult"]',
    '[class*="search-item"]',
)


# =============================================================================
# 辅助
# =============================================================================


def first_present(page_or_locator, candidates: tuple[str, ...], *, timeout_ms: int = 0):
    """按顺序尝试候选选择器，返回第一个能命中的 locator。

    ``timeout_ms=0`` 表示只做一次即时检查，不等待 —— 用于「哪个备用选择器生效了」
    这类探测场景，避免每个候选都要等一遍超时。

    全都没命中时返回 ``None``。调用方需要自己决定这是致命错误还是可以降级。
    """
    for selector in candidates:
        try:
            locator = page_or_locator.locator(selector).first
            if timeout_ms > 0:
                locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            if locator.count() > 0:
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def any_present(page_or_locator, candidates: tuple[str, ...]) -> bool:
    """任意一个候选选择器命中即为真。用于页面类型判定。"""
    return first_present(page_or_locator, candidates) is not None


def describe_candidates(candidates: tuple[str, ...], limit: int = 3) -> str:
    """把候选选择器列表渲染成给人看的一行，用于报错信息。

    报错里带上试过哪些选择器，用户和未来改代码的人才能快速判断是改版了
    还是别的原因。
    """
    shown = candidates[:limit]
    text = " / ".join(shown)
    if len(candidates) > limit:
        text += f" …（共 {len(candidates)} 个候选）"
    return text
