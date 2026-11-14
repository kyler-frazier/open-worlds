import { useId, useRef, useState } from "react";

interface Props {
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** Blocks *editing* only. Revealing stays available -- see the note below. */
  disabled?: boolean;
  placeholder?: string;
  hint?: React.ReactNode;
  /** Rendered under the field when the value is empty, to say what that means. */
  emptyHint?: React.ReactNode;
  /**
   * The value already saved on the server, if any. Supplying it makes the field
   * read-then-edit (masked until the pencil is clicked); omit it for a plain
   * input, as on the create form.
   */
  savedValue?: string;
}

/**
 * A password input that starts masked and can be revealed, since a join
 * password is only useful if the owner can read it back.
 *
 * Reveal ignores `disabled`: the edit modal disables fields on any world that
 * is not OFF, and reading a value is not changing it.
 *
 * With `savedValue`, editing starts from empty rather than over a masked value
 * you cannot see. Empty on save removes the password, so backing out restores
 * the original.
 */
export default function SecretField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  hint,
  emptyHint,
  savedValue,
}: Props) {
  const hasSaved = savedValue !== undefined && savedValue !== "";
  const [editing, setEditing] = useState(!hasSaved);
  const [revealed, setRevealed] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();

  // A world that gains or loses its password while the modal is open (someone
  // else saved, or a poll refreshed it) should not leave the field in a mode
  // that no longer makes sense. Reset during render, not in an effect, so the
  // stale mode is never painted.
  const [prevSaved, setPrevSaved] = useState(savedValue);
  if (savedValue !== prevSaved) {
    setPrevSaved(savedValue);
    setEditing(!hasSaved);
  }

  const startEditing = () => {
    setEditing(true);
    onChange("");
    // Focus after the input becomes writable, or the click lands on nothing.
    requestAnimationFrame(() => input.current?.focus());
  };

  const cancelEditing = () => {
    setEditing(false);
    setRevealed(false);
    onChange(savedValue ?? "");
  };

  // In read mode the box shows what is stored, not the staged value -- they
  // are the same until the pencil is clicked, and after that the staged value
  // is empty and belongs in the edit box.
  const shown = editing ? value : (savedValue ?? "");

  return (
    <label htmlFor={id}>
      {label}
      <span className="secret-field">
        <input
          id={id}
          ref={input}
          type={revealed ? "text" : "password"}
          value={shown}
          disabled={disabled && editing}
          readOnly={!editing}
          placeholder={editing ? placeholder : undefined}
          // These are shared room passwords, not account credentials: browser
          // password managers offering to save them is noise.
          autoComplete="off"
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          // Describes what the click does, which is what a screen reader user
          // needs; the visible label describes the same action.
          aria-label={revealed ? "Hide password" : "Show password"}
          aria-pressed={revealed}
          onClick={() => setRevealed((r) => !r)}
        >
          {revealed ? "Hide" : "Show"}
        </button>
        {hasSaved &&
          (editing ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label="Keep the current password"
              onClick={cancelEditing}
            >
              Keep
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label="Change password"
              title="Change password"
              disabled={disabled}
              onClick={startEditing}
            >
              ✎
            </button>
          ))}
      </span>
      {hint && <span className="hint muted">{hint}</span>}
      {hasSaved && editing && !value && (
        <span className="hint muted">
          Type a new password, or leave this blank to remove it and reopen the
          world. <strong>Keep</strong> leaves the current one alone.
        </span>
      )}
      {!shown && emptyHint && <span className="hint muted">{emptyHint}</span>}
    </label>
  );
}
