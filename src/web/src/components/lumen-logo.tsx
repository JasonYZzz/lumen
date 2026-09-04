import { useId } from 'react'

type LumenMarkProps = {
  className?: string
  title?: string
}

export function LumenMark({ className, title }: LumenMarkProps) {
  const labelled = Boolean(title)
  const instanceId = useId().replace(/:/g, '')
  const gradientId = `lumen-fold-gradient-${instanceId}`
  const maskId = `lumen-fold-mask-${instanceId}`
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      role={labelled ? 'img' : undefined}
      aria-label={title}
      aria-hidden={labelled ? undefined : true}
    >
      <defs>
        <linearGradient id={gradientId} x1="12" y1="7" x2="56" y2="58" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFD16A" />
          <stop offset=".48" stopColor="#F39A1D" />
          <stop offset="1" stopColor="#B85A0A" />
        </linearGradient>
        <mask id={maskId} maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">
          <rect width="64" height="64" fill="#fff" />
          <path
            d="M30 31c-.4 5.5-2.7 9-5.8 12.8-3.6 4.4-3.3 9.2.2 13.5-3.9-2.4-5.6-5.6-4.4-9.2 1.1-3.3 4.7-6.9 6.8-9.9 2-2.8 2.8-5.5 3.2-7.2Z"
            fill="#000"
          />
        </mask>
      </defs>
      <path
        className="lumen-fold-shape"
        d="M20 5C13.4 5 8 10.4 8 17v25c0 9.9 8.1 18 18 18h22c6.6 0 12-5.4 12-12s-5.4-12-12-12H32V17c0-6.6-5.4-12-12-12Z"
        fill={`url(#${gradientId})`}
        mask={`url(#${maskId})`}
      />
    </svg>
  )
}

export function LumenLogo({ className }: { className?: string }) {
  return (
    <span className={className ? `lumen-logo ${className}` : 'lumen-logo'} aria-label="Lumen">
      <LumenMark className="lumen-logo-mark" />
      <span className="lumen-logo-wordmark" aria-hidden="true">lumen</span>
    </span>
  )
}
