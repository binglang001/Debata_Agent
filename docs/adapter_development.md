# 渠道适配器开发指南

如何接入新的聊天平台（Discord / Telegram / 飞书 / QQ 频道等）。

---

## 核心契约

实现 `adapters/base.py` 的 `IAdapter` 抽象基类。所有适配器一视同仁。

```python
from adapters.base import IAdapter
from adapters.types import Target, IncomingMessage, FriendInfo, GroupInfo

class MyAdapter(IAdapter):
    def __init__(self, name: str, config: MyAdapterConfig):
        super().__init__(name)
```

## 必实现的方法

### 生命周期
```python
async def start(self) -> None
async def stop(self) -> None
@property
def is_connected(self) -> bool
```

### 消息发送
```python
async def send_text(self, target: Target, content: str) -> str | None
async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None) -> str | None
async def recall(self, message_id: str) -> bool
```

### 联系人
```python
async def list_friends(self) -> list[FriendInfo]
async def list_groups(self) -> list[GroupInfo]
async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]
async def get_user_info(self, user_id: str) -> UserInfo
```

### 请求
```python
async def handle_friend_request(self, flag, approve, remark="")
async def handle_group_request(self, flag, sub_type, approve, reason="")
```

### 兜底
```python
async def call_api(self, action: str, **params) -> dict
```

## 可选实现（默认抛 NotImplementedError）

```python
async def fetch_voice_text(self, message_id) -> str
async def get_file_url(self, file_id) -> str | None
async def upload_file(self, target, file_path, *, display_name=None)
async def get_forward_msg(self, forward_id) -> list[dict]
async def get_group_history(self, group_id, count=100) -> list[dict]
```

## 事件投递

收到平台事件 → 转成 `adapters/types.py` 的统一类型 → 调 `self._emit(event)`：

```python
from adapters.types import IncomingMessage, MediaSegment, MediaType

event = IncomingMessage(
    adapter=self.name,
    timestamp=time.time(),
    self_id=str(self.bot_id),
    message_id=str(msg.id),
    scope="private",                # 或 "group"
    user_id=str(msg.user_id),
    nickname=msg.nickname,
    group_id=str(msg.group_id) if group else None,
    text=msg.text,
    raw_message=msg.raw_text,
    media=[MediaSegment(type=MediaType.IMAGE, url="https://...")],
    reply_to=str(msg.reply_to_id) if msg.reply_to_id else None,
    raw=msg.to_dict(),
)
await self._emit(event)
```

同理 `IncomingNotice`（撤回/入群）/ `IncomingRequest`（验证）/ `MetaEvent`（心跳）。

## 参考：NapCatAdapter

`adapters/napcat/` 是完整实现：

- `connection.py` - 反向 WS + 正向 WS
- `api_call.py` - echo 配对（asyncio Future）
- `events.py` - JSON → 统一事件
- `process.py` - 可选托管 NapCat 进程
- `adapter.py` - 拼装 IAdapter

## Discord 骨架示例

```python
import discord
from adapters.base import IAdapter
from adapters.types import Target, IncomingMessage, MediaSegment, MediaType

class DiscordAdapter(IAdapter):
    def __init__(self, name, config):
        super().__init__(name)
        self.client = discord.Client(intents=...)

        @self.client.event
        async def on_message(msg):
            if msg.author.bot:
                return
            event = IncomingMessage(
                adapter=self.name, timestamp=msg.created_at.timestamp(),
                self_id=str(self.client.user.id), message_id=str(msg.id),
                scope="group" if msg.guild else "private",
                user_id=str(msg.author.id), nickname=msg.author.display_name,
                group_id=str(msg.guild.id) if msg.guild else None,
                text=msg.content, raw_message=msg.content,
                media=[MediaSegment(type=MediaType.IMAGE, url=a.url)
                       for a in msg.attachments if a.content_type.startswith("image/")],
                raw=msg.to_dict(),
            )
            await self._emit(event)

    async def send_text(self, target, content):
        if target.scope == "private":
            user = await self.client.fetch_user(int(target.target_id))
            sent = await user.send(content)
        else:
            channel = self.client.get_channel(int(target.target_id))
            sent = await channel.send(content)
        return str(sent.id)
```

## CQ 码兼容

工具系统输出 `[CQ:reply,id=...]` / `[CQ:at,qq=...]`。如平台不支持 CQ 码（如 Discord），在 `send_text` 里转换：

```python
async def send_text(self, target, content):
    content = self._cq_to_native(content)  # [CQ:at,qq=123] → <@123>
```

## 配置 schema

`app_config/schema.py`:

```python
class MyAdapterConfig(StrictModel):
    type: Literal["my_adapter"] = "my_adapter"
    enabled: bool = True
    bot_token_id: str | None = None

AdapterConfig = Annotated[
    NapCatAdapterConfig | MyAdapterConfig,
    Field(discriminator="type"),
]
```

## 测试

```python
@pytest.fixture
def adapter():
    a = MyAdapter("test", cfg)
    a._client = FakeMyPlatform()
    return a

@pytest.mark.asyncio
async def test_send_text(adapter):
    msg_id = await adapter.send_text(Target("test", "private", "123"), "hi")
    assert msg_id == "..."
```

参考 `tests/test_adapters_napcat.py`。

## 提交 PR

- ✅ 实现 `IAdapter` 所有抽象方法
- ✅ 单元测试 send / receive / 联系人 / 请求
- ✅ 配置 schema 加好
- ✅ 向导文案 `ui/wizard/copy.py` + `steps.py` 加渠道选项
- ✅ 不修改 `IAdapter` 抽象（除非破坏性变更经讨论）
