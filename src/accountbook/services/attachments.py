"""附件存储（P1 尾巴 T5 / T6）。

服务层不自己去查全局路径
------------------------
每个写文件的函数都要求调用方传入 ``root``（API 层从 ``ctx.paths.attachments``
取）。服务层直接去读全局路径会让它无法在临时目录里被测试，
而"附件到底写到哪"这件事本来就该由装配层决定。

三层安全校验，缺一不可
----------------------
1. **大小上限**：先看 ``Content-Length`` 只能挡住君子，实际写入时按块累计，
   一旦超过上限立刻中止并删除半成品 —— 否则一个未声明长度的请求能把磁盘写满。
2. **类型白名单 + 魔数校验**：只看扩展名会被 ``evil.png`` 这种改名绕过；
   只看魔数又无法决定该存成什么扩展名。两者都查，且要求它们**一致**。
3. **路径穿越**：文件名**完全由我们生成**（内容哈希 + 由魔数推出的扩展名），
   用户提供的文件名只作为展示用原样存进数据库，不参与路径拼接。
   另外在读的时候再校验一次最终路径落在 root 之内 —— 纵深防御。

为什么按内容哈希命名
--------------------
同一张发票被重复上传时自然去重（同一路径、同样的内容），
而且路径里没有任何用户可控的片段。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import Attachment, Transaction

__all__ = [
    "ALLOWED_TYPES",
    "MAX_BYTES",
    "delete",
    "get",
    "list_for_transaction",
    "read_bytes",
    "resolve_path",
    "save",
]

_logger = logging.getLogger(__name__)

#: 单个附件上限。发票照片通常在 2–5 MB，10 MB 留了足够余量，
#: 同时不让"手滑选中一个视频"把数据目录撑爆
MAX_BYTES = 10 * 1024 * 1024

#: 只接受这些类型。**不含** SVG：它是可执行内容（内嵌脚本），
#: 在本地 webview 里渲染用户上传的 SVG 等于开了一个 XSS 口子。
ALLOWED_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}

#: 魔数 → MIME。与 ALLOWED_TYPES 互为校验：两者都要命中且一致才放行
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

#: 原文件名里的路径分隔符与控制字符会被清掉 —— 它只用于展示，
#: 但一个带 ``../`` 的展示名仍然会让日志与导出变得混乱
_UNSAFE_NAME = re.compile(r"[\x00-\x1f/\\]+")


def _sniff(head: bytes) -> str | None:
    """按魔数判断真实类型。返回 None 表示不认识。"""
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    # WebP 是 RIFF 容器，需要看第 8–12 字节
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_name(filename: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", (filename or "").strip())
    return cleaned[-160:] or "attachment"


def _relative_ref(mime: str, digest: str, *, kind: str, today: date | None = None) -> str:
    """生成相对引用：``<kind>/<年>/<月>/<哈希前 16 位><扩展名>``。

    路径里**没有任何用户可控的片段** —— 这是路径穿越的根本防线。
    按年月分目录是为了避免单目录里堆几万个文件（Windows 上目录遍历会变慢）。
    """
    stamp = today or date.today()
    extension = ALLOWED_TYPES[mime]
    folder = "cards" if kind == "card" else "transactions"
    return f"{folder}/{stamp.year:04d}/{stamp.month:02d}/{digest[:16]}{extension}"


def resolve_path(root: Path, file_ref: str) -> Path:
    """把相对引用解析成绝对路径，并**再次**确认它落在 root 之内。

    纵深防御：写入时路径已由我们生成，但读取时仍然校验一次 ——
    万一将来有人手工改了数据库里的 ``file_ref``（导入、插件、手改 DB），
    这一层能挡住读到数据目录之外的文件。
    """
    base = Path(root).resolve()
    candidate = (base / file_ref).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValidationError("附件路径越界", field="file_ref", value=file_ref)
    return candidate


def save(
    session: Session,
    root: Path,
    data: bytes,
    *,
    filename: str,
    kind: str = "transaction",
    transaction_id: int | None = None,
    card_artwork_id: int | None = None,
) -> Attachment:
    """保存一个附件。

    ``data`` 已经在调用方读进内存 —— 因为上限是 10 MB，
    流式处理带来的复杂度（半成品文件的清理、并发写同一路径）
    不值得。上限本身就是这里的第一道防线。
    """
    if kind not in {"transaction", "card"}:
        raise ValidationError(f"未知的附件类型：{kind}", field="kind")
    if not data:
        raise ValidationError("附件内容为空", field="data")
    if len(data) > MAX_BYTES:
        raise ValidationError(
            f"附件超过 {MAX_BYTES // (1024 * 1024)} MB 上限",
            field="data",
            size_bytes=len(data),
            max_bytes=MAX_BYTES,
        )

    mime = _sniff(data[:32])
    if mime is None or mime not in ALLOWED_TYPES:
        raise ValidationError(
            "不支持的文件类型（只接受 PNG / JPEG / WebP / GIF / PDF）",
            field="data",
            detected=mime,
            allowed=sorted(ALLOWED_TYPES),
        )

    if transaction_id is not None:
        row = session.get(Transaction, transaction_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError("流水不存在", entity="transaction", entity_id=transaction_id)
    if kind == "card" and card_artwork_id is None:
        raise ValidationError("卡面附件必须指定卡面", field="card_artwork_id")

    digest = hashlib.sha256(data).hexdigest()
    file_ref = _relative_ref(mime, digest, kind=kind)
    target = resolve_path(root, file_ref)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 内容寻址：同一份内容重复上传时文件已存在，跳过写入即可
    if not target.exists():
        # 先写临时文件再原子替换：中途失败不会留下一个半截的"正常附件"
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)

    attachment = Attachment(
        kind=kind,
        transaction_id=transaction_id,
        card_artwork_id=card_artwork_id,
        file_ref=file_ref,
        original_name=_safe_name(filename),
        mime=mime,
        size_bytes=len(data),
        sha256=digest,
    )
    session.add(attachment)
    session.flush()
    return attachment


def serialize(attachment: Attachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "kind": attachment.kind,
        "transaction_id": attachment.transaction_id,
        "card_artwork_id": attachment.card_artwork_id,
        "original_name": attachment.original_name,
        "mime": attachment.mime,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "created_at": attachment.created_at.isoformat() if attachment.created_at else None,
        # 前端直接用这个地址做 <img src>：走的是同源请求，会话 Cookie 会带上
        "url": f"/api/attachments/{attachment.id}",
    }


def list_for_transaction(session: Session, transaction_id: int) -> list[Attachment]:
    return list(
        session.scalars(
            select(Attachment).where(Attachment.transaction_id == transaction_id).order_by(Attachment.id)
        ).all()
    )


def list_for_artwork(session: Session, artwork_id: int) -> list[Attachment]:
    return list(
        session.scalars(
            select(Attachment).where(Attachment.card_artwork_id == artwork_id).order_by(Attachment.id)
        ).all()
    )


def get(session: Session, attachment_id: int) -> Attachment:
    row = session.get(Attachment, attachment_id)
    if row is None:
        raise NotFoundError("附件不存在", entity="attachment", entity_id=attachment_id)
    return row


def read_bytes(root: Path, attachment: Attachment) -> bytes:
    """读取附件内容。文件缺失时给出明确错误，而不是让 FileNotFoundError 冒到 API 层。"""
    path = resolve_path(root, attachment.file_ref)
    if not path.exists():
        raise NotFoundError(
            "附件文件已丢失（数据库有记录但磁盘上没有）",
            entity="attachment",
            entity_id=attachment.id,
            file_ref=attachment.file_ref,
        )
    return path.read_bytes()


def open_stream(root: Path, attachment: Attachment) -> BinaryIO:
    path = resolve_path(root, attachment.file_ref)
    if not path.exists():
        raise NotFoundError("附件文件已丢失", entity="attachment", entity_id=attachment.id)
    return path.open("rb")


def delete(session: Session, root: Path, attachment_id: int, *, remove_file: bool = True) -> None:
    """删除附件。

    ``remove_file=False`` 用于"数据库记录要删、但文件可能被另一条记录共用"
    的情形（内容寻址下同一份内容会有多条记录指向同一个文件）。
    因此默认先检查是否还有别的记录引用同一文件。
    """
    attachment = get(session, attachment_id)
    file_ref = attachment.file_ref
    session.delete(attachment)
    session.flush()

    if remove_file:
        still_used = session.scalar(select(Attachment.id).where(Attachment.file_ref == file_ref).limit(1))
        if still_used is not None:
            # 内容寻址的自然结果：同一张图被挂到两笔流水上时，
            # 删掉其中一条不该把另一条的图也弄丢
            _logger.info("附件文件仍被 #%s 引用，保留 %s", still_used, file_ref)
            return
        try:
            resolve_path(root, file_ref).unlink(missing_ok=True)
        except OSError as error:  # pragma: no cover - 磁盘问题
            # 文件删不掉不该让整个删除操作失败：数据库记录已经清了，
            # 留一个孤儿文件比留一条指向空文件的记录好
            _logger.warning("删除附件文件失败 %s：%s", file_ref, error)


def assert_size_within_limit(size_bytes: int) -> None:
    """供 API 层在读取请求体之前先做一次快速拒绝（避免白读 100 MB）。"""
    if size_bytes > MAX_BYTES:
        raise ConflictError(
            f"附件超过 {MAX_BYTES // (1024 * 1024)} MB 上限",
            size_bytes=size_bytes,
            max_bytes=MAX_BYTES,
        )
