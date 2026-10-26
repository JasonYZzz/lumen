import { useEffect, useRef, useState, type CSSProperties } from "react";

import ashenPressSource from "./sources/ashen-press.html?raw";
export { ashenPressSource };

export type AshenPressProps = {
  className?: string;
  style?: CSSProperties;
  source?: string;
  onOpenResource?: (id: string) => void;
};

export function AshenPress({ className = "", style, source, onOpenResource }: AshenPressProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [documentVisible, setDocumentVisible] = useState(() => (
    typeof document === "undefined" || !document.hidden
  ));
  const [hostVisible, setHostVisible] = useState(true);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== frameRef.current?.contentWindow || !event.data || typeof event.data !== 'object') return;
      if (event.data.type === 'lumen:shelf-ready') setReady(true);
      if (event.data.type === 'lumen:shelf-error') setFailed(true);
      if (event.data.type === 'lumen:open-resource' && typeof event.data.id === 'string') onOpenResource?.(event.data.id);
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, [onOpenResource]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      setHostVisible(entry?.isIntersecting ?? true);
    }, { rootMargin: "80px" });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const update = () => setDocumentVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  const mounted = hostVisible && documentVisible;

  useEffect(() => {
    setReady(false);
    setFailed(false);
  }, [mounted, source]);

  return (
    <div
      ref={hostRef}
      className={`threeui-background ashen-press${className ? ` ${className}` : ""}`}
      role="group"
      aria-label={source ? "Lumen 3D 资料书架" : "Interactive Ashen Press art book shelf"}
      data-state={!mounted ? "paused" : failed ? "failed" : ready ? "ready" : "loading"}
      style={{
        position: "relative",
        overflow: "hidden",
        background: "#c6ae8e",
        pointerEvents: "auto",
        ...style,
      }}
    >
      {source && (!ready || failed) && <p className="library-scene-status" role={failed ? 'alert' : 'status'}>
        {failed ? '3D 书架暂时无法加载，请切换到网格或列表继续浏览。' : '正在布置资料书架…'}
      </p>}
      {mounted && !failed ? (
        <iframe
          ref={frameRef}
          title={source ? "Lumen 资料书架" : "Ashen Press — The Art Book Shelf"}
          srcDoc={source ?? ashenPressSource}
          sandbox="allow-scripts"
          loading="eager"
          onLoad={() => { if (!source) setReady(true); }}
          style={{
            position: "absolute",
            inset: 0,
            display: "block",
            width: "100%",
            height: "100%",
            border: 0,
            background: source ? 'var(--page)' : "#c6ae8e",
            opacity: ready ? 1 : 0,
            pointerEvents: ready ? "auto" : "none",
            transition: "opacity 240ms ease-out",
          }}
        />
      ) : null}
    </div>
  );
}
