/**
 * 附件（P1 尾巴 T5 / T6）。
 *
 * 一个必须说清楚的状态：**新流水还没有 id，因此加不了附件**。
 * 与其把上传按钮disabled得莫名其妙，不如直接写明"保存后才能添加附件"——
 * 用户会知道该先做什么。
 *
 * 打开附件用 `window.open` 而不是 `<a download>`：图片与 PDF 在
 * webview 里能直接内联显示与查看，而强制下载对"我只想看一眼发票"是更差的体验。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { Icon } from '@/components/Icon'
import { useI18n } from '@/i18n'
import { api, ApiError, type AttachmentItem } from '@/lib/api'

/** 人类可读的体积 */
function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function isImage(item: AttachmentItem): boolean {
  return item.mime.startsWith('image/')
}

/**
 * 流水的附件区。
 *
 * ``transactionId`` 为 ``null`` 表示这是一笔还没保存的流水。
 */
export function TransactionAttachments({ transactionId }: { transactionId: number | null }) {
  const { t } = useI18n()
  const [items, setItems] = useState<AttachmentItem[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    if (transactionId === null) {
      setItems([])
      return
    }
    try {
      setItems(await api.transactionAttachments(transactionId))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [transactionId])

  useEffect(() => {
    void load()
  }, [load])

  const upload = async (file: File) => {
    if (transactionId === null) return
    setBusy(true)
    setError(null)
    try {
      await api.uploadTransactionAttachment(transactionId, file)
      await load()
    } catch (cause) {
      // 服务端会说清是"超过 10 MB"还是"不支持的文件类型"，
      // 这句原话比界面自己编的"上传失败"有用
      setError(cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  if (transactionId === null) {
    return (
      <div>
        <span className="ab-field-label">{t('attachments.title')}</span>
        <p className="text-ab-caption1 text-label-3">{t('attachments.saveFirst')}</p>
      </div>
    )
  }

  return (
    <div>
      <span className="ab-field-label">{t('attachments.title')}</span>
      <div className="space-y-1.5">
        {items.map((item) => (
          <div key={item.id} className="flex items-center gap-2 rounded-ab-sm bg-surface-2 px-2 py-1.5">
            {isImage(item) ? (
              <img
                src={item.url}
                alt={item.original_name}
                className="h-8 w-8 shrink-0 rounded-[6px] object-cover"
              />
            ) : (
              <Icon name="note" size={14} className="shrink-0 text-label-3" />
            )}
            <button
              type="button"
              className="min-w-0 flex-1 truncate text-left text-ab-footnote text-label-2 hover:text-accent"
              onClick={() => window.open(item.url, '_blank')}
            >
              {item.original_name}
            </button>
            <span className="ab-tnum shrink-0 text-ab-caption1 text-label-3">{humanSize(item.size_bytes)}</span>
            <button
              type="button"
              className="ab-icon-btn shrink-0 hover:text-negative"
              aria-label={t('common.delete')}
              onClick={() => {
                void api.deleteAttachment(item.id).then(() => load())
              }}
            >
              <Icon name="trash" size={12} />
            </button>
          </div>
        ))}

        <input
          ref={inputRef}
          type="file"
          className="hidden"
          // 与服务端白名单一致：SVG 不要（它是可执行内容）
          accept="image/png,image/jpeg,image/webp,image/gif,application/pdf"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void upload(file)
          }}
        />
        <button
          type="button"
          className="ab-btn-secondary"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
        >
          <Icon name="plus" size={13} />
          {busy ? t('attachments.uploading') : t('attachments.add')}
        </button>
        <p className="text-ab-caption1 text-label-3">{t('attachments.hint')}</p>
        {error ? <p className="text-ab-caption1 text-negative">{error}</p> : null}
      </div>
    </div>
  )
}

/**
 * 卡面图片上传（T6）。
 *
 * 上传成功后回到卡片墙会用 `image_url` 渲染真实图片；没有图片时仍用
 * spec 里的自绘渐变 —— 两条路都通，用户不必为"想用图片"而放弃自绘。
 */
export function CardImageUpload({
  artworkId,
  imageUrl,
  onUploaded,
}: {
  artworkId: number
  imageUrl: string | null
  onUploaded: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  return (
    <div>
      <span className="ab-field-label">{t('attachments.cardImage')}</span>
      <div className="flex items-center gap-2">
        {imageUrl ? (
          <img src={imageUrl} alt="" className="h-10 w-16 shrink-0 rounded-[6px] object-cover" />
        ) : (
          <span className="flex h-10 w-16 shrink-0 items-center justify-center rounded-[6px] bg-surface-2 text-ab-caption2 text-label-3">
            {t('attachments.noImage')}
          </span>
        )}
        <input
          ref={inputRef}
          type="file"
          className="hidden"
          accept="image/png,image/jpeg,image/webp"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (!file) return
            setBusy(true)
            setError(null)
            void api
              .uploadCardImage(artworkId, file)
              .then(() => onUploaded())
              .catch((cause: unknown) =>
                setError(cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause)),
              )
              .finally(() => {
                setBusy(false)
                if (inputRef.current) inputRef.current.value = ''
              })
          }}
        />
        <button
          type="button"
          className="ab-btn-secondary"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
        >
          <Icon name="plus" size={13} />
          {busy ? t('attachments.uploading') : t('attachments.uploadImage')}
        </button>
      </div>
      <p className="mt-1 text-ab-caption1 text-label-3">{t('attachments.cardImageHint')}</p>
      {error ? <p className="text-ab-caption1 text-negative">{error}</p> : null}
    </div>
  )
}
