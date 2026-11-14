import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeGame } from "../test/fixtures";
import SettingsFields from "./SettingsFields";

const game = makeGame({
  settings_schema: {
    properties: {
      difficulty: {
        title: "Difficulty",
        enum: ["easy", "hard"],
        default: "easy",
      },
      view_distance: { title: "View distance", type: "integer", default: 10 },
      hardcore: { title: "Hardcore", type: "boolean", default: false },
      motd: {
        title: "Message of the day",
        type: "string",
        description: "Shown in the server list.",
      },
    },
  },
  spec_fields: ["difficulty"],
});

describe("SettingsFields", () => {
  it("builds the form from the schema, defaults filled in", () => {
    render(<SettingsFields game={game} settings={{}} onChange={() => {}} />);
    expect(screen.getByLabelText("Difficulty")).toHaveValue("easy");
    expect(screen.getByLabelText("View distance")).toHaveValue(10);
    expect(screen.getByLabelText("Hardcore")).not.toBeChecked();
    expect(screen.getByText("Shown in the server list.")).toBeInTheDocument();
  });

  it("reports each change with its type", async () => {
    const onChange = vi.fn();
    render(
      <SettingsFields
        game={game}
        settings={{ motd: "hi" }}
        onChange={onChange}
      />,
    );
    await userEvent.selectOptions(screen.getByLabelText("Difficulty"), "hard");
    expect(onChange).toHaveBeenLastCalledWith({
      motd: "hi",
      difficulty: "hard",
    });
    await userEvent.click(screen.getByLabelText("Hardcore"));
    expect(onChange).toHaveBeenLastCalledWith({ motd: "hi", hardcore: true });
    await userEvent.type(screen.getByLabelText("View distance"), "2");
    expect(onChange).toHaveBeenLastCalledWith({
      motd: "hi",
      view_distance: 102,
    });
  });

  it("locks spec fields only while asked to", () => {
    const { rerender } = render(
      <SettingsFields
        game={game}
        settings={{}}
        onChange={() => {}}
        specLocked
      />,
    );
    expect(screen.getByLabelText("Difficulty")).toBeDisabled();
    expect(screen.getByLabelText("Hardcore")).toBeEnabled();
    rerender(<SettingsFields game={game} settings={{}} onChange={() => {}} />);
    expect(screen.getByLabelText("Difficulty")).toBeEnabled();
  });

  it("renders nothing for a game without settings", () => {
    const { container } = render(
      <SettingsFields game={makeGame()} settings={{}} onChange={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
