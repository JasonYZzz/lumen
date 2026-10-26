export class AuthenticationError extends Error {
  constructor() {
    super('浏览器登录已失效，请从 Lumen 启动链接重新连接。')
    this.name = 'AuthenticationError'
  }
}

export function checkAuthentication(response: Response) {
  if (response.status !== 401) return
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('lumen:authentication-required'))
  throw new AuthenticationError()
}
