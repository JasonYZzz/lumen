import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: process.env.NODE_ENV === 'production' ? 'export' : undefined,
  images: { unoptimized: true },
}

if (process.env.NODE_ENV !== 'production') {
  nextConfig.rewrites = async () => {
    const api = process.env.LUMEN_API_URL
    if (!api) return []
    return [
      { source: '/api/:path*', destination: `${api}/api/:path*` },
      { source: '/auth/:path*', destination: `${api}/auth/:path*` },
    ]
  }
}

export default nextConfig
