import type { Metadata, Viewport } from 'next'
import { GeistSans } from 'geist/font/sans'
import './globals.css'

export const metadata: Metadata = {
  title: 'Lumen',
  description: 'Lumen coding agent',
}

export const viewport: Viewport = {
  colorScheme: 'light',
  themeColor: '#f7f7f7',
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className={GeistSans.variable}>
      <body>{children}</body>
    </html>
  )
}
