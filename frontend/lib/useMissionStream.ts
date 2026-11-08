"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
import { fromMission, missionReducer } from "./reducer";
import type { MissionState } from "./reducer";
import { streamUrl } from "./config";
import { MissionStream } from "./stream";
import type { Connection } from "./stream";
import type { Mission } from "./types";

/**
 * Follow a mission live. Give the component that calls this a `key` of the mission id, so
 * opening a different mission starts from a clean state.
 */
export function useMissionStream(id: string) {
  const [state, dispatch] = useReducer(missionReducer, null as MissionState | null);
  const [connection, setConnection] = useState<Connection>("connecting");

  useEffect(() => {
    const stream = new MissionStream({
      url: streamUrl(id),
      onMessage: (message) => dispatch({ type: "message", message }),
      onConnection: setConnection,
    });
    stream.start();
    return () => stream.stop();
  }, [id]);

  /** Use a mission read some other way (for example the reply to cancelling) as the latest state. */
  const applyMission = useCallback(
    (mission: Mission) => dispatch({ type: "message", message: fromMission(mission) }),
    [],
  );

  return { state, connection, applyMission };
}
