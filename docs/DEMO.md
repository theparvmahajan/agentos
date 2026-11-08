# Recording the demo gif

Goal: a gif under about 8 MB that shows the system doing its job in 30 to 40 seconds. It goes in
`docs/media/demo.gif` and the README already has the line for it (uncomment it).

## What to show (shot list)

1. (0-3 s) The empty dashboard at `http://localhost:8080`.
2. (3-8 s) Type the objective, press Start. Log in once if asked (cut that out when editing).
3. (8-20 s) The graph appears: researcher tasks fan out in parallel, a coder node, a reviewer node at
   the end. Nodes turn from queued to running to done at different times: **this is the point**.
4. (20-28 s) Click the coder task: the panel shows the code and the output it really produced.
5. (28-35 s) The mission header turns Completed; the result card shows the final answer.
6. Optional second clip: start a mission, press Cancel while it runs, show the remaining tasks stop.

A good objective makes the graph fan out and include code. Try something like:
`Compare two sorting algorithms: research how quicksort and mergesort differ, write and run Python that
times both on 100000 random numbers, and review the findings.`
Run it once beforehand: small local models sometimes plan badly, and the take you keep should be a
real, successful run (do not stage or edit the output).

## Recording

- Browser window about 1280x720, light theme, zoom 100 to 110%, nothing private in other tabs.
- Windows: ScreenToGif (free): record the window at 10 to 12 fps, trim, "Save as gif".
- Any OS: record an mp4 (OBS or the OS recorder), then:

```bash
ffmpeg -i demo.mp4 -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" docs/media/demo.gif
```

If it is too big: lower `fps` to 8, `scale` to 800, or shorten the take. A screenshot of the finished
mission (`docs/media/mission.png`) is a good second image.

Before you commit it, check that no API key, password or private path is visible in any frame.
