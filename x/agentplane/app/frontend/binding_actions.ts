/** What a binding row may do to its binding, and what each of those does — the row's own words. */
import type { BindingView } from "./client";

export type BindingAction = "approve" | "deny" | "revoke";

export type ActionOffer = {
  action: BindingAction;
  explains: string;
  /** Why the button is dead, or null when it can be pressed. */
  blocked: string | null;
};

// Flux applies the whole spec of a binding it owns, and `approval` is a required field, so a seed's
// approval is declared in git as much as its existence: an approval written here would stand only
// until the next reconcile, and the API refuses one with 409.
const FROM_GIT =
  "Applied by Flux, which declares its approval too — change it in git, or the next reconcile undoes it.";

const EXPLANATIONS: Record<BindingAction, string> = {
  approve: "Lets the sandbox reach what these policies allow, and records who decided.",
  deny: "Grants nothing while it stands, but keeps the binding and who decided; approving again restores it.",
  revoke: "Deletes the binding and the record of who decided. There is no undo; a new binding has to be made.",
};

/** The approval a binding is not already in, plus revoking; every one of them git's if it came from git. */
export function bindingActions(binding: BindingView): ActionOffer[] {
  const actions: BindingAction[] = [];
  if (binding.approval !== "approved") actions.push("approve");
  if (binding.approval !== "denied") actions.push("deny");
  actions.push("revoke");
  return actions.map((action) => ({
    action,
    explains: EXPLANATIONS[action],
    blocked: binding.from_git ? FROM_GIT : null,
  }));
}
