# odour.py
'''
Writen by Johnston lab 2025
Designed to work with a NIDAQ-6001
Records 2 channels of analog in data at 1kHz with live display
'''
import sys, time, csv, threading
import numpy as np
import nidaqmx
from nidaqmx.constants import AcquisitionType, TerminalConfiguration # MODIFIED: Added TerminalConfiguration
from nidaqmx.system import System
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg
import os 
import re 


class Record(QtWidgets.QWidget): # CHANGED: Class name changed from SmoothDAQViewer to Record
    acquisition_state_changed = QtCore.pyqtSignal(bool)
    acquisition_error = QtCore.pyqtSignal(str)

    """
    Continuous NI-DAQ data viewer using a ring buffer for smooth plotting and
    a separate thread for full-rate data acquisition and logging.
    """
    def __init__(self, sample_rate=1000, samples_per_read=100, window_sec=5, filename="Data/data.csv"):
        super().__init__()
        self.setWindowTitle("NI-DAQ Data Recorder") # CHANGED: Updated window title

        # --- Configuration and State ---
        self.fs = sample_rate          # Sampling rate (Hz)
        self.read_n = samples_per_read # Samples acquired per read call
        self.win_sec = window_sec      # Time window to display on plot (seconds)
        self.running = False           # Flag for acquisition thread state
        self.save_data = False         # True when actively recording to CSV
        self.reader = None             # Acquisition thread handle
        self._buffer_lock = threading.Lock()

        # --- NI-DAQ Setup ---
        # Detect the first available device (e.g., 'Dev1')
        try:
            self.dev = System.local().devices[0].name
        except IndexError:
             raise RuntimeError("No NI-DAQ device found.")
        print(f"Using NI-DAQ device: {self.dev}")

        self.task = nidaqmx.Task()
        # Add two analog input channels (ai0 and ai1)
        # MODIFIED: Added terminal_config=TerminalConfiguration.RSE to both channels
        self.task.ai_channels.add_ai_voltage_chan(
            f"{self.dev}/ai0", 
            max_val=10.0, 
            min_val=-10.0,
            terminal_config=TerminalConfiguration.RSE
        )
        self.task.ai_channels.add_ai_voltage_chan(
            f"{self.dev}/ai1", 
            max_val=10.0, 
            min_val=-10.0,
            terminal_config=TerminalConfiguration.RSE
        )

        # Configure continuous sampling timing
        self.task.timing.cfg_samp_clk_timing(
            self.fs,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=self.read_n
        )

        # --- Ring Buffer Initialization ---
        # The buffer holds 2x the display window size for safe wrap-around handling
        self.buflen = int(self.fs * self.win_sec * 2)
        # 2 channels (rows), buffer length (columns)
        self.data = np.zeros((2, self.buflen))
        self.time = np.zeros(self.buflen)
        self.ptr = 0 # Current total samples acquired (index pointer)

        # --- UI Setup (Vertical Layout) ---
        main_layout = QtWidgets.QVBoxLayout(self)

        # Control layout (Horizontal)
        ctrl_layout = QtWidgets.QHBoxLayout()
        self.file_edit = QtWidgets.QLineEdit(filename)
        self.status_label = QtWidgets.QLabel("Status: Idle")
        self.preview_btn = QtWidgets.QPushButton("Preview")
        self.start_btn = QtWidgets.QPushButton("Start Acquisition")
        self.stop_btn = QtWidgets.QPushButton("Stop Acquisition")
        self.stop_btn.setEnabled(False)

        ctrl_layout.addWidget(QtWidgets.QLabel("Save Data To:"))
        ctrl_layout.addWidget(self.file_edit)
        ctrl_layout.addWidget(self.status_label)
        ctrl_layout.addWidget(self.preview_btn)
        ctrl_layout.addWidget(self.start_btn)
        ctrl_layout.addWidget(self.stop_btn)
        main_layout.addLayout(ctrl_layout)

        # Plot Widget
        self.plot_widget = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.plot_widget)

        # Plot 1 (Channel 0) - Top Plot
        self.p1 = self.plot_widget.addPlot(title="Channel 0 (ai0)")
        self.p1.showGrid(x=True, y=True)
        self.c1 = self.p1.plot(pen='y')

        # Move to the next row for vertical stacking (Crucial for vertical arrangement)
        self.plot_widget.nextRow()

        # Plot 2 (Channel 1) - Bottom Plot
        self.p2 = self.plot_widget.addPlot(title="Channel 1 (ai1)")
        self.p2.showGrid(x=True, y=True)
        self.c2 = self.p2.plot(pen='c')

        # --- Connections and Timer ---
        self.preview_btn.clicked.connect(self.preview)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.acquisition_state_changed.connect(self._on_acquisition_state_changed)
        self.acquisition_error.connect(self._on_acquisition_error)

        # Timer to periodically update the plot (GUI refresh rate, e.g., 20 FPS)
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(50)
        
    def _find_next_filename(self, base_filename):
        """
        Finds the next available filename by appending a zero-padded index (e.g., _000, _001, _010).
        It first strips any existing index from the filename to ensure correct sequential numbering.
        """
        # Split filename into root and extension (e.g., 'data' and '.csv')
        root, ext = os.path.splitext(base_filename)
        
        # FIX: Strip any existing _XXX index from the root name (e.g., 'data_001' -> 'data')
        # This ensures the numbering always starts from the clean base name.
        root = re.sub(r'_\d{3}$', '', root)
        
        # MODIFIED: Start the index search from 0 (for data_000.csv)
        index = 0
        while index < 1000: # Limit check to prevent indefinite loop
            # Use f-string formatting to zero-pad the index to three digits
            new_filename = f"{root}_{index:03d}{ext}"
            if not os.path.exists(new_filename):
                return new_filename
            index += 1
            
        # Fallback if 1000 indexed files already exist
        return base_filename 

    # --- Acquisition Thread Logic ---
    def acq_loop(self):
        """Runs in a separate thread to continuously read NI-DAQ data and optionally log to CSV."""
        writer_obj = None
        try:
            if self.save_data:
                # The filename is guaranteed to be unique thanks to start()
                writer_obj = open(self.file_edit.text(), "w", newline="")
                w = csv.writer(writer_obj)
                w.writerow(["Time(s)", "Ch0(V)", "Ch1(V)"])

            while self.running:
                # Read a block of samples (2 channels x self.read_n samples)
                block = np.array(self.task.read(number_of_samples_per_channel=self.read_n))

                # Reshape if only one channel was read (safety check, though 2 are configured)
                if block.ndim == 1:
                    block = block[np.newaxis, :]

                n = block.shape[1]
                # Calculate time array for the new block relative to the start of acquisition
                t = (np.arange(n) + self.ptr) / self.fs

                # --- Ring Buffer Write Logic (Wrap-around handling) ---
                with self._buffer_lock:
                    i0 = self.ptr % self.buflen # Start index in buffer
                    i1 = (i0 + n) % self.buflen # End index in buffer

                    if i1 > i0:
                        # Simple case: No wrap-around
                        self.data[:, i0:i1] = block
                        self.time[i0:i1] = t
                    else:
                        # Wrap-around case: Data splits into two segments
                        k = self.buflen - i0
                        # Segment 1: from i0 to end of buffer
                        self.data[:, i0:] = block[:, :k]
                        self.time[i0:] = t[:k]
                        # Segment 2: from start of buffer to i1
                        self.data[:, :i1] = block[:, k:]
                        self.time[:i1] = t[k:]

                    self.ptr += n # Advance the total sample pointer

                if self.save_data:
                    # Save full-rate data to CSV
                    # Using np.transpose to iterate rows efficiently
                    w.writerows(zip(t, *block))
        except Exception as e:
            print(f"Acquisition Error: {e}")
            self.acquisition_error.emit(str(e))
        finally:
            if writer_obj is not None:
                writer_obj.close()
            print("Acquisition thread ended.")
            # Ensure UI reflects stopped state if error occurred
            self.running = False
            self.save_data = False
            self.acquisition_state_changed.emit(False)

    def _start_acquisition(self, save_data):
        """Starts acquisition in either preview mode or recording mode."""
        if self.running or (self.reader is not None and self.reader.is_alive()):
            return

        self.save_data = save_data
        if self.save_data:
            # Determine the unique filename and update the QLineEdit display
            new_filename = self._find_next_filename(self.file_edit.text())
            self.file_edit.setText(new_filename)
            self.status_label.setText("Status: Recording")
            print(f"Acquisition started, saving to {self.file_edit.text()}")
        else:
            self.status_label.setText("Status: Preview")
            print("Preview started (display only, no file will be written).")

        self.running = True
        self.ptr = 0 # Reset buffer pointer on start
        self.acquisition_state_changed.emit(True)

        # Start the background data reader thread
        self.reader = threading.Thread(target=self.acq_loop, daemon=True)
        self.reader.start()

    def preview(self):
        """Starts a live preview without recording data to disk."""
        self._start_acquisition(save_data=False)

    def start(self):
        """Starts the NI-DAQ task and acquisition thread with CSV recording enabled."""
        self._start_acquisition(save_data=True)


    def stop(self):
        """Sets the flag to stop the acquisition thread."""
        if not self.running:
            return
        self.status_label.setText("Status: Stopping")
        self.running = False
        self.save_data = False
        self.stop_btn.setEnabled(False)
        # Note: The thread will finish its current read and exit the loop gracefully.


    def update_plot(self):
        """Updates the pyqtgraph plot with the latest data from the ring buffer."""
        if not self.running:
            return

        with self._buffer_lock:
            # Calculate the size of the data window to display
            nwin = int(self.fs * self.win_sec)
            end = self.ptr % self.buflen # Current end index in the buffer
            start = (end - nwin) % self.buflen # Start index for the display window

            # --- Ring Buffer Read Logic (Wrap-around handling) ---
            if start < end:
                # Simple case: Data is contiguous
                x = self.time[start:end].copy()
                y0 = self.data[0, start:end].copy()
                y1 = self.data[1, start:end].copy()
            else:
                # Wrap-around case: Concatenate the end and start segments
                x = np.concatenate((self.time[start:], self.time[:end]))
                y0 = np.concatenate((self.data[0, start:], self.data[0, :end]))
                y1 = np.concatenate((self.data[1, start:], self.data[1, :end]))

        # Decimate the data for faster plotting if there are too many points
        decim = max(1, len(x)//2000)
        x_decim = x[::decim]
        y0_decim = y0[::decim]
        y1_decim = y1[::decim]

        # Update plot data
        self.c1.setData(x_decim, y0_decim)
        self.c2.setData(x_decim, y1_decim)

        # Autoscroll the X-axis to show the latest window
        if x.size > 0:
            for p in (self.p1, self.p2):
                p.setXRange(x[-1] - self.win_sec, x[-1])

    @QtCore.pyqtSlot(bool)
    def _on_acquisition_state_changed(self, running):
        self.file_edit.setReadOnly(running)
        self.preview_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        if not running and not self.status_label.text().startswith("Status: Error"):
            self.status_label.setText("Status: Idle")

    @QtCore.pyqtSlot(str)
    def _on_acquisition_error(self, error_message):
        self.status_label.setText(f"Status: Error - {error_message}")
        QtWidgets.QMessageBox.critical(self, "Acquisition Error", error_message)


    def closeEvent(self, event):
        """
        Called when the application window is closed. Ensures the acquisition 
        thread is stopped and the NI-DAQ task is closed properly.
        """
        print("Application closing: Stopping DAQ task and thread...")
        self.stop()
        # Give the thread a moment to finish its current read and exit loop
        if self.reader is not None and self.reader.is_alive():
            self.reader.join()

        # Essential: Close the NI-DAQ task handle to release resources
        self.task.close()
        event.accept()


def main():
    """Main application entry point."""
    app = QtWidgets.QApplication(sys.argv)
    w = Record(sample_rate=1000, samples_per_read=100, window_sec=5) # CHANGED: Class instantiation
    w.resize(1000, 600)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
