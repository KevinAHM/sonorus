# Complete tkinter mock for embedded Python (Hogwarts Legacy wwiser)
class MockWidget:
    def __init__(self, *args, **kwargs): pass
    def pack(self, *args, **kwargs): return self
    def grid(self, *args, **kwargs): return self
    def place(self, *args, **kwargs): return self
    def configure(self, *args, **kwargs): return self
    def cget(self, key): return None
    def destroy(self): pass
    def update(self): pass
    def focus(self): pass

class MockFont:
    def __init__(self, **kwargs): pass
    def configure(self, **kwargs): return self
    def copy(self): return MockFont()
    def actual(self): return {'family': 'Arial', 'size': 10}

class MockFileDialog:
    @staticmethod
    def askopenfilename(): return ""
    @staticmethod
    def asksaveasfilename(): return ""
    @staticmethod
    def askdirectory(): return ""

class MockScrolledText(MockWidget): pass

class MockMessageBox:
    @staticmethod
    def showerror(*args): print("ERROR:", args[0] if args else "")
    @staticmethod
    def showinfo(*args): print("INFO:", args[0] if args else "")
    @staticmethod
    def askyesno(*args): return True
    @staticmethod
    def showwarning(*args): print("WARNING:", args[0] if args else "")

class MockStyle:
    def __init__(self): pass
    def configure(self, *args, **kwargs): pass
    def map(self, *args, **kwargs): return []
    def theme_use(self, *args, **kwargs): pass

class MockTTK:
    Frame = MockWidget
    Style = MockStyle
    Label = MockWidget
    Button = MockWidget
    Entry = MockWidget
    Combobox = MockWidget
    Notebook = MockWidget
    # Add more widgets as errors appear

# Monkey-patch sys.modules BEFORE tkinter import
import sys
sys.modules['tkinter.font'] = MockFont()
sys.modules['tkinter.filedialog'] = MockFileDialog
sys.modules['tkinter.scrolledtext'] = MockScrolledText
sys.modules['tkinter.messagebox'] = MockMessageBox
sys.modules['tkinter.ttk'] = MockTTK()

# Now tkinter import succeeds with full widget support
from tkinter import ttk, font, filedialog, scrolledtext, messagebox