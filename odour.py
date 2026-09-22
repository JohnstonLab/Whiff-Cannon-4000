# odour.py
'''
Writen by Johnston lab 2025
Designed to work with a NIDAQ-6001
Runs in jupyterlab
'''

import asyncio
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import ipywidgets as widgets
import nidaqmx

class Gun:
    NUM_VALVES = 12
    REQUIRED_TRIAL_COLUMNS = ("Pre (s)", "Stim (s)", "Vial")
    TRIGGER_PULSE_S = 0.02

    def __init__(self, odours, trials=None):
        # -------------------------------
        # Save user data
        # -------------------------------
        self.odours = odours
        self.trials = trials
        self.running = False
        self.FLOW = 0
        self._run_task = None
        self._record_process = None
        self._record_script = Path(__file__).with_name("record.py")

        # -------------------------------
        # Detect NI-DAQ devices
        # -------------------------------
        devices = [device.name for device in nidaqmx.system.System.local().devices]
        if not devices:
            raise RuntimeError("No NI-DAQ devices detected. Please connect a device.")
        self.device_name = devices[0]
        print(f"Using NI-DAQ device: {self.device_name}")

        # -------------------------------
        # Setup digital output channels
        # -------------------------------
        self.gunValves = nidaqmx.Task()
        for i in range(8):
            self.gunValves.do_channels.add_do_chan(f"{self.device_name}/port0/line{i}")
        for i in range(4):
            self.gunValves.do_channels.add_do_chan(f"{self.device_name}/port1/line{i}")
        
        self.flowValve = nidaqmx.Task()
        self.flowValve.do_channels.add_do_chan(f"{self.device_name}/port2/line0")

        # -------------------------------
        # Setup analog output channel
        # -------------------------------
        self.gunAO = nidaqmx.Task()
        self.gunAO.ao_channels.add_ao_voltage_chan(f"{self.device_name}/ao0")

        self.triggerAO = nidaqmx.Task()
        self.triggerAO.ao_channels.add_ao_voltage_chan(f"{self.device_name}/ao1")
        self.triggerAO.write(0)

        # -------------------------------
        # GUI widgets
        # -------------------------------
        self.txSize = widgets.Layout(width='60%', height='45px')
        self.buttonSize = widgets.Layout(width='60%', height='50px')

        self.FlowOn = widgets.Button(description='Flow On')
        self.FlowOn.on_click(self.flowStart)

        self.Stopflow = widgets.Button(description='Stop', layout=self.buttonSize, style={'button_color':'red'})
        self.Stopflow.on_click(self.stop)

        self.StartRecord = widgets.Button(description='Open Recorder')
        self.StartRecord.on_click(self.start_recorder)
        self.StopRecord = widgets.Button(description='Close Recorder', style={'button_color':'lightgray'})
        self.StopRecord.on_click(self.stop_recorder)

        self.OdourList = widgets.Dropdown(options=self.odours, value=self.odours[0],
                                         description='Test vial:', disabled=False, layout=self.txSize)

        self.Run = widgets.Button(description='Run Sequence', style={'button_color':'lightgreen'}, layout=self.buttonSize)
        self.Run.on_click(self.running_seq)

        self.RunTest = widgets.Button(description='Run Test', style={'button_color':'lightgreen'}, layout=self.buttonSize)
        self.RunTest.on_click(self.running_test)

        self.StimDur = widgets.BoundedFloatText(value=3, min=0.02, max=120, description='Stim time:', layout=self.txSize)
        self.PreStim = widgets.BoundedFloatText(value=3, min=0, description='Pre Stim:', layout=self.txSize)
        self.UseTrigger = widgets.Checkbox(value=False, description='Use trigger', indent=False)

        self.prog = widgets.IntProgress(value=0, min=0, max=10, step=1, orientation='horizontal',
                                        layout=widgets.Layout(width='100%'))
        self.Status = widgets.Label(value='Status: Stopped', layout=widgets.Layout(width='100%', height='50px'))

    # -------------------------------
    # Valve control functions
    # -------------------------------
    def flow_on(self):
        d = np.ones(1)
        self.flowValve.write(d == 1)
        self.FLOW = 1
        self.gunAO.write(1)
        # print(f"Valve {v} ON, AO={(v + 1) / 2}")

    def _validate_vial_index(self, v):
        if not 0 <= int(v) < self.NUM_VALVES:
            raise ValueError(f"Valve index must be between 0 and {self.NUM_VALVES - 1}. Got: {v}")

    def _set_run_state(self, is_running):
        self.running = is_running
        self.Run.disabled = is_running
        self.RunTest.disabled = is_running

    def _validate_trials(self):
        if self.trials is None:
            raise ValueError("No trials loaded. Provide a trial DataFrame before running the sequence.")
        missing = [c for c in self.REQUIRED_TRIAL_COLUMNS if c not in self.trials.columns]
        if missing:
            raise ValueError(f"Trials missing required columns: {missing}")

        trial_df = self.trials.copy()
        if "Post (s)" not in trial_df.columns:
            trial_df["Post (s)"] = 0
            print("Protocol missing 'Post (s)' column; assuming 0 s post time.")

        for col in ("Pre (s)", "Stim (s)", "Post (s)", "Vial"):
            trial_df[col] = pd.to_numeric(trial_df[col], errors="raise")
        if (trial_df["Vial"] < 1).any() or (trial_df["Vial"] > self.NUM_VALVES).any():
            raise ValueError(f"'Vial' values must be in range 1-{self.NUM_VALVES}.")
        self.trials = trial_df

    def _start_task(self, coro, status):
        if self._run_task is not None and not self._run_task.done():
            self.Status.value = "Status: Already running. Stop current run before starting a new one."
            return

        self.Status.value = status
        self._set_run_state(True)
        try:
            # ensure_future is more tolerant in notebook event-loop contexts.
            self._run_task = asyncio.ensure_future(coro)
            self._run_task.add_done_callback(self._on_task_done)
        except Exception as exc:
            try:
                coro.close()
            except Exception:
                pass
            self._set_run_state(False)
            self._run_task = None
            self.Status.value = f"Status: Error - could not start task ({exc})"

    def _on_task_done(self, task):
        self._set_run_state(False)
        self._run_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.Status.value = f"Status: Error - {exc}"

    def toggle_on(self, v):
        self._validate_vial_index(v)
        d = np.zeros(self.NUM_VALVES)
        d[v] = 1
        self.gunValves.write(d == 1)
        if self.FLOW == 1:
            self.gunAO.write(((v + 1) / 3)+1)
        else:
            self.gunAO.write(((v + 1) / 3))
        # print(f"Valve {v} ON, AO={(v + 1) / 2}")

    def toggle_off(self):
        d = np.zeros(self.NUM_VALVES)
        self.gunValves.write(d == 1)
        if self.FLOW == 1:
            self.gunAO.write(1)
        else:
            self.gunAO.write(0)
        # print(f"Valve {v} OFF, AO={(v + 1) / 2}")

    def toggle_all_off(self):
        d = np.zeros(self.NUM_VALVES)
        self.gunValves.write(d == 1)
        d =np.zeros(1)
        self.flowValve.write(d == 1)
        self.gunAO.write(0)
        self.triggerAO.write(0)
        self.FLOW = 0
        return 'All valves closed and flow stopped'

    # -------------------------------
    # Async sequence functions
    # -------------------------------
    async def trigger_pulse(self):
        if not self.UseTrigger.value:
            return
        self.triggerAO.write(5)
        await asyncio.sleep(self.TRIGGER_PULSE_S)
        self.triggerAO.write(0)

    async def test_vial(self):
        vial = self.odours.index(self.OdourList.value)

        await self.trigger_pulse()
        await asyncio.sleep(self.PreStim.value)
        if not self.running: return
        self.toggle_on(vial)
        await asyncio.sleep(self.StimDur.value)
        if not self.running: return
        self.toggle_off()

    async def sequencer(self):
        self.prog.max = len(self.trials)
        self.prog.value = 0

        for i in range(len(self.trials)):
            print(str(self.trials.loc[[i]]).split('\n')[1])
            self.Status.value = str(self.trials.loc[[i]]).split('\n')[1]

            await self.trigger_pulse()
            await asyncio.sleep(self.trials.loc[i,('Pre (s)')])
            if not self.running: return
            self.toggle_on(int(self.trials.loc[i,('Vial')])-1)
            await asyncio.sleep(self.trials.loc[i,('Stim (s)')])
            if not self.running: return
            self.toggle_off()
            await asyncio.sleep(self.trials.loc[i,('Post (s)')])
            if not self.running: return
            self.prog.value = i + 1
        self.Status.value = 'Status: Finished'

    # -------------------------------
    # Button callbacks
    # -------------------------------
    def flowStart(self, arg):
        self.flow_on()
        self.Status.value = "Status: Flow on"

    def stop(self, arg):
        self.running = False
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        self._set_run_state(False)
        self.Status.value = "Status: " + self.toggle_all_off()

    def running_seq(self, arg):
        try:
            self._validate_trials()
        except Exception as exc:
            self.Status.value = f"Status: Error - {exc}"
            return
        self._start_task(self.sequencer(), 'Status: Running sequence')

    def running_test(self, arg):
        self._start_task(self.test_vial(), 'Status: Running test')

    def start_recorder(self, arg):
        if not self._record_script.exists():
            self.Status.value = f"Status: Recorder script not found: {self._record_script.name}"
            return
        if self._record_process is not None and self._record_process.poll() is None:
            self.Status.value = "Status: Recorder already running"
            return
        self._record_process = subprocess.Popen(
            [sys.executable, str(self._record_script)],
            cwd=str(self._record_script.parent),
        )
        self.Status.value = "Status: Recorder opened"

    def stop_recorder(self, arg):
        if self._record_process is None or self._record_process.poll() is not None:
            self.Status.value = "Status: Recorder is not running"
            self._record_process = None
            return
        self._record_process.terminate()
        try:
            self._record_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._record_process.kill()
            self._record_process.wait(timeout=3)
        self._record_process = None
        self.Status.value = "Status: Recorder closed"

    def close(self):
        self.stop_recorder(None)
        self.toggle_all_off()
        self.gunValves.close()
        self.flowValve.close()
        self.gunAO.close()
        self.triggerAO.close()

    # -------------------------------
    # GUI display function
    # -------------------------------
    def gui(self):
        box_layout = widgets.Layout(width='100%', height='100%', align_items='center', border='1px solid green')
        timeBox = widgets.VBox([widgets.Label(value="Time selection (s)"), self.PreStim, self.StimDur], layout=box_layout)
        TestBox = widgets.VBox([widgets.Label(value="Test Odour"), self.OdourList, self.RunTest], layout=widgets.Layout(align_items='center', border='1px solid green'))
        StimBox = widgets.VBox([widgets.Label(value="Controls"), self.UseTrigger, self.Run, self.Stopflow], layout=widgets.Layout(align_items='center', border='1px solid green'))
        RecorderRow = widgets.HBox([widgets.Label(value="Recorder:"), self.StartRecord, self.StopRecord], layout=widgets.Layout(align_items='center', justify_content='center', border='1px solid green'))

        grid = widgets.GridspecLayout(6, 3, grid_gap='10px', height='100%')
        grid[0, :] = RecorderRow
        grid[1:4, 0] = timeBox
        grid[1:4, 1] = TestBox
        grid[1:4, 2] = StimBox
        grid[4, 0] = self.FlowOn
        grid[4, 1] = self.prog
        grid[4, 2] = self.Status
        return grid


# Backwards compatibility for existing notebook usage.
gun = Gun
