export async function renderPoster(node: HTMLElement): Promise<Blob> {
  await document.fonts.ready;
  const { toBlob } = await import("html-to-image");
  const width = node.scrollWidth || 650;
  const height = node.scrollHeight || node.offsetHeight;
  const blob = await toBlob(node, {
    width,
    height,
    pixelRatio: 3,
    backgroundColor: "#FAF8F3",
    skipFonts: true,
    style: { transform: "none" },
  });
  if (!blob) throw new Error("长图生成失败。");
  return blob;
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("图片编码失败。"));
    reader.readAsDataURL(blob);
  });
}

export async function copyPoster(node: HTMLElement): Promise<void> {
  const blob = await renderPoster(node);
  if (window.finresearch?.clipboard?.writeImageDataUrl) {
    const ok = await window.finresearch.clipboard.writeImageDataUrl(await blobToDataUrl(blob));
    if (ok) return;
  }
  if (!navigator.clipboard || typeof ClipboardItem === "undefined") {
    throw new Error("当前环境无法写入图片剪贴板，请使用「保存高清 PNG」。");
  }
  await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
}

export function savePoster(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "景渊-" + name.replace(/[<>:"/\\|?*]/g, "_").slice(0, 64) + ".png";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 120_000);
}
