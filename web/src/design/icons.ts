/**
 * 图标目录 —— 供「图标选择器」使用的分组清单。
 *
 * 为什么单独成文件
 * ----------------
 * `Icon.tsx` 只负责"怎么画"，这里负责"有哪些、怎么分类给用户挑"。
 * 二者变更节奏不同：加一个新图标要先改 `Icon.tsx`，
 * 决定它出现在选择器的哪一组才改这里。混在一起会让选择器逻辑越来越重。
 *
 * 选择器按语义分组而不是字母序 —— 用户在找"餐饮"图标时，
 * 会先想到"这属于生活类"，而不是"它英文叫 food"。
 */

/** 选择器分组：一组一个主题，组内按常见程度排序 */
export interface IconGroup {
  /** 分组标识（用于 i18n key：design.iconGroup.<id>） */
  id: string
  icons: string[]
}

/** 记账分类图标（支出方向为主） */
export const CATEGORY_ICON_GROUPS: IconGroup[] = [
  {
    id: 'food',
    icons: ['food', 'snack', 'coffee', 'takeout', 'dining', 'groceries'],
  },
  {
    id: 'transport',
    icons: ['transport', 'bus', 'taxi', 'fuel', 'parking', 'toll', 'train', 'plane', 'bike', 'car'],
  },
  {
    id: 'home',
    icons: ['home', 'bank', 'building', 'water', 'bolt', 'flame', 'heater', 'wifi', 'sofa', 'tools'],
  },
  {
    id: 'shopping',
    icons: ['shopping', 'shirt', 'basket', 'cosmetics', 'device', 'baby', 'gift'],
  },
  {
    id: 'health',
    icons: ['health', 'hospital', 'pill', 'stethoscope', 'tooth'],
  },
  {
    id: 'education',
    icons: ['education', 'book', 'graduation', 'exam', 'pen'],
  },
  {
    id: 'leisure',
    icons: ['leisure', 'film', 'game', 'travel', 'dumbbell', 'recurring', 'heart'],
  },
  {
    id: 'life',
    icons: ['communication', 'phone', 'signal', 'pet', 'bone', 'vet', 'childcare', 'toy'],
  },
  {
    id: 'money',
    icons: ['finance', 'percent', 'interest', 'receipt', 'shield', 'alert', 'work', 'briefcase', 'other'],
  },
  {
    id: 'income',
    icons: ['salary', 'award', 'overtime', 'subsidy', 'business', 'parttime', 'shop', 'investment', 'chart', 'dividend', 'transfer_in', 'refund'],
  },
]

/** 账户图标（含支付渠道） */
export const ACCOUNT_ICON_GROUPS: IconGroup[] = [
  {
    id: 'wallet',
    icons: ['cash', 'wallet', 'accounts', 'piggy', 'cards', 'bank'],
  },
  {
    id: 'channel',
    icons: ['wechat', 'alipay', 'phone', 'device'],
  },
  {
    id: 'asset',
    icons: ['investment', 'chart', 'kline', 'dividend', 'goals'],
  },
  {
    id: 'debt',
    icons: ['debts', 'percent', 'receipt', 'briefcase'],
  },
  {
    id: 'other',
    icons: ['shield', 'gift', 'tag', 'other'],
  },
]

/** 全部可选图标（去重，供校验用） */
export const ALL_PICKABLE_ICONS: string[] = Array.from(
  new Set([...CATEGORY_ICON_GROUPS, ...ACCOUNT_ICON_GROUPS].flatMap((group) => group.icons)),
)
