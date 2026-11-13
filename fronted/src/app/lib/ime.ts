export function isImeKeyEvent(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229 || event.key === "Process";
}
