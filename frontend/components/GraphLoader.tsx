"use client";

import dynamic from "next/dynamic";

// The graph needs the browser (it measures the screen), so it is not rendered on the server.
const GraphView = dynamic(() => import("./GraphView"), {
  ssr: false,
  loading: () => (
    <div className="graph-placeholder" role="status">
      Loading graph…
    </div>
  ),
});

export default GraphView;
