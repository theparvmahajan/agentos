import type { Mission, MissionMeta, StreamMessage, Task } from "./types";

/** What the page holds for one mission. Tasks keep the order the server gave them (plan order). */
export interface MissionState {
  mission: MissionMeta;
  tasks: Task[];
}

/**
 * Apply one stream message. Every message carries a complete current state (not a delta), so
 * applying a message twice, or an old one after a newer one, can only ever show a state the
 * mission really had, and the next message corrects it: see docs/API.md.
 */
export function applyMessage(state: MissionState | null, message: StreamMessage): MissionState | null {
  switch (message.type) {
    case "snapshot": {
      const { tasks, ...mission } = message.mission;
      return { mission, tasks };
    }
    case "task": {
      if (!state || state.mission.id !== message.mission_id) return state;
      const index = state.tasks.findIndex((t) => t.key === message.task.key);
      const tasks =
        index === -1
          ? [...state.tasks, message.task] // a task we have not seen: a new node
          : state.tasks.map((t, i) => (i === index ? message.task : t));
      return { ...state, tasks };
    }
    case "mission": {
      if (!state || state.mission.id !== message.mission.id) return state;
      return { ...state, mission: { ...state.mission, ...message.mission } };
    }
  }
}

export type Action = { type: "message"; message: StreamMessage };

export function missionReducer(state: MissionState | null, action: Action): MissionState | null {
  return applyMessage(state, action.message);
}

export function fromMission(mission: Mission): StreamMessage {
  return { type: "snapshot", mission };
}
