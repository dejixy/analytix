/* A side drawer over the dashboard, or the same content as a plain panel on a page (the terminal's tabs). */
export default function Drawer({ inline, className = "", label, onClose, children }) {
  if (inline) return <section className={`planner inline ${className}`} aria-label={label}>{children}</section>;
  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className={`planner ${className}`} role="dialog" aria-label={label} onClick={(e) => e.stopPropagation()}>
        {children}
      </aside>
    </div>
  );
}
