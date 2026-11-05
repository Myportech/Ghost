import sys
from AppKit import (
    NSApplication,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSEventMaskKeyDown,
    NSFocusRingTypeNone,
    NSFont,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSWindowStyleMaskBorderless,
    NSPanel,
)
from PyObjCTools import AppHelper
import objc


class KeyPanel(NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class BarView(NSView):
    def drawRect_(self, rect):
        bounds = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 14, 14)
        NSColor.colorWithSRGBRed_green_blue_alpha_(24 / 255, 24 / 255, 27 / 255, 1).set()
        path.fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(63 / 255, 63 / 255, 70 / 255, 1).set()
        path.setLineWidth_(1.5)
        path.stroke()


def main():
    app = NSApplication.sharedApplication()

    w, h = 520.0, 54.0
    panel = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (w, h)),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    screen = NSScreen.mainScreen().visibleFrame()
    panel.setFrameOrigin_((
        screen.origin.x + (screen.size.width - w) / 2.0,
        screen.origin.y + (screen.size.height - h) * 0.72,
    ))
    panel.setLevel_(NSStatusWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setOpaque_(False)
    panel.setHasShadow_(True)
    panel.setMovableByWindowBackground_(True)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )

    content = BarView.alloc().initWithFrame_(((0, 0), (w, h)))
    panel.setContentView_(content)

    label = NSTextField.alloc().initWithFrame_(((16, 14), (28, 26)))
    label.setStringValue_(">")
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setFont_(NSFont.systemFontOfSize_(18))
    label.setTextColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.63, 0.63, 0.67, 1))
    content.addSubview_(label)

    field = NSTextField.alloc().initWithFrame_(((46, 12), (w - 62, 30)))
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setBordered_(False)
    field.setFocusRingType_(NSFocusRingTypeNone)
    field.setFont_(NSFont.fontWithName_size_("Menlo", 16) or NSFont.systemFontOfSize_(16))
    field.setTextColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.957, 0.957, 0.961, 1))
    field.setPlaceholderString_("...")
    content.addSubview_(field)

    def current_text():
        editor = field.currentEditor()
        if editor is not None:
            return str(editor.string() or "")
        return str(field.stringValue() or "")

    def finish(text):
        sys.stdout.write(text)
        sys.stdout.flush()
        AppHelper.stopEventLoop()

    def on_key(event):
        code = event.keyCode()
        if code in (36, 76):
            finish(current_text())
            return None
        if code == 53:
            AppHelper.stopEventLoop()
            return None
        return event

    NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, on_key)

    def focus(_=None):
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)
        panel.makeFirstResponder_(field)
        editor = panel.fieldEditor_forObject_(True, field)
        if editor is not None:
            editor.setInsertionPointColor_(NSColor.whiteColor())
        if panel.isKeyWindow():
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        sys.stderr.write(f"active={bool(app.isActive())} key={bool(panel.isKeyWindow())}\n")
        sys.stderr.flush()

    panel.orderFrontRegardless()
    focus()
    AppHelper.callLater(0.25, focus)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
