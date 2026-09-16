import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.tuthan.steamoscompanion"

  readonly property var panelObject: panelLoader.item
  readonly property bool opened: panelObject ? panelObject.opened === true : false
  readonly property string statusState: panelObject ? panelObject.statusState : "unpaired"
  readonly property string statusWord: panelObject ? panelObject.statusWord : "Not paired"
  readonly property bool paired: panelObject ? panelObject.paired === true : false
  readonly property bool statusNeedsAttention: panelObject ? panelObject.statusNeedsAttention === true : false
  readonly property bool statusUncertain: panelObject ? panelObject.statusUncertain === true : false
  readonly property string hostLabel: panelObject ? panelObject.hostLabel : "SteamOS Companion"
  readonly property string lastObservation: panelObject ? panelObject.lastObservation : "not observed"
  readonly property string barTooltip: panelObject ? panelObject.barTooltip : "SteamOS Companion · not paired"
  readonly property bool popoutSwitchClosing: panelObject ? panelObject.popoutSwitchClosing === true : false

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function open() { if (panelObject) panelObject.open() }
  function close() { if (panelObject) panelObject.close() }
  function toggle() { if (panelObject) panelObject.toggle() }
  function closeForPopoutSwitch() { if (panelObject) panelObject.closeForPopoutSwitch() }
  function refresh() { if (panelObject) panelObject.refresh() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  IpcHandler {
    target: root.moduleName
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    iconComponent: Component {
      Image {
        anchors.fill: parent
        source: Qt.resolvedUrl("assets/steamos-companion-icon.svg")
        sourceSize.width: width
        sourceSize.height: height
        fillMode: Image.PreserveAspectFit
        smooth: true
      }
    }
    // Attention is reserved for something actually observed: a paired host that
    // refused or dropped the last request. A fresh install has no problem to
    // report, and a reading that has aged out while the panel was closed is an
    // absence of knowledge rather than a failure. Both of those dim instead of
    // lighting up, and the tooltip says which of the three it is.
    active: root.statusNeedsAttention
    dimmed: !root.paired || root.statusUncertain
    tooltipText: root.barTooltip

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.refresh()
      else if (buttonCode === Qt.LeftButton) root.toggle()
      else if (buttonCode === Qt.RightButton) root.open()
    }
  }
}
