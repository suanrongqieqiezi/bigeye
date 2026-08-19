var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
import JQuery from 'jquery';
import { Dom, FunctionExt } from '../util';
import { Cell } from '../model';
import { Config } from '../global';
import { View, Markup } from '../view';
export class GraphView extends View {
    constructor(graph) {
        super();
        this.graph = graph;
        const { selectors, fragment } = Markup.parseJSONMarkup(GraphView.markup);
        this.background = selectors.background;
        this.grid = selectors.grid;
        this.svg = selectors.svg;
        this.defs = selectors.defs;
        this.viewport = selectors.viewport;
        this.primer = selectors.primer;
        this.stage = selectors.stage;
        this.decorator = selectors.decorator;
        this.overlay = selectors.overlay;
        this.container = this.options.container;
        this.restore = GraphView.snapshoot(this.container);
        this.$(this.container)
            .addClass(this.prefixClassName('graph'))
            .append(fragment);
        this.delegateEvents();
    }
    get model() {
        return this.graph.model;
    }
    get options() {
        return this.graph.options;
    }
    delegateEvents() {
        const ctor = this.constructor;
        super.delegateEvents(ctor.events);
        return this;
    }
    /**
     * Guard the specified event. If the event is not interesting, it
     * returns `true`, otherwise returns `false`.
     */
    guard(e, view) {
        // handled as `contextmenu` type
        if (e.type === 'mousedown' && e.button === 2) {
            return true;
        }
        if (this.options.guard && this.options.guard(e, view)) {
            return true;
        }
        if (e.data && e.data.guarded !== undefined) {
            return e.data.guarded;
        }
        if (view && view.cell && Cell.isCell(view.cell)) {
            return false;
        }
        if (this.svg === e.target ||
            this.container === e.target ||
            JQuery.contains(this.svg, e.target)) {
            return false;
        }
        return true;
    }
    findView(elem) {
        return this.graph.renderer.findViewByElem(elem);
    }
    onDblClick(evt) {
        if (this.options.preventDefaultDblClick) {
            evt.preventDefault();
        }
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
        if (view) {
            view.onDblClick(e, localPoint.x, localPoint.y);
        }
        else {
            this.graph.trigger('blank:dblclick', {
                e,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
    }
    onClick(evt) {
        if (this.getMouseMovedCount(evt) <= this.options.clickThreshold) {
            const e = this.normalizeEvent(evt);
            const view = this.findView(e.target);
            if (this.guard(e, view)) {
                return;
            }
            const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
            if (view) {
                view.onClick(e, localPoint.x, localPoint.y);
            }
            else {
                this.graph.trigger('blank:click', {
                    e,
                    x: localPoint.x,
                    y: localPoint.y,
                });
            }
        }
    }
    isPreventDefaultContextMenu(evt, view) {
        let preventDefaultContextMenu = this.options.preventDefaultContextMenu;
        if (typeof preventDefaultContextMenu === 'function') {
            preventDefaultContextMenu = FunctionExt.call(preventDefaultContextMenu, this.graph, { view });
        }
        return preventDefaultContextMenu;
    }
    onContextMenu(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.isPreventDefaultContextMenu(e, view)) {
            evt.preventDefault();
        }
        if (this.guard(e, view)) {
            return;
        }
        const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
        if (view) {
            view.onContextMenu(e, localPoint.x, localPoint.y);
        }
        else {
            this.graph.trigger('blank:contextmenu', {
                e,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
    }
    delegateDragEvents(e, view) {
        if (e.data == null) {
            e.data = {};
        }
        this.setEventData(e, {
            currentView: view || null,
            mouseMovedCount: 0,
            startPosition: {
                x: e.clientX,
                y: e.clientY,
            },
        });
        const ctor = this.constructor;
        this.delegateDocumentEvents(ctor.documentEvents, e.data);
        this.undelegateEvents();
    }
    getMouseMovedCount(e) {
        const data = this.getEventData(e);
        return data.mouseMovedCount || 0;
    }
    onMouseDown(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        if (this.options.preventDefaultMouseDown) {
            e.preventDefault();
        }
        const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
        if (view) {
            view.onMouseDown(e, localPoint.x, localPoint.y);
        }
        else {
            if (this.options.preventDefaultBlankAction &&
                ['touchstart'].includes(e.type)) {
                e.preventDefault();
            }
            this.graph.trigger('blank:mousedown', {
                e,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
        this.delegateDragEvents(e, view);
    }
    onMouseMove(evt) {
        const data = this.getEventData(evt);
        const startPosition = data.startPosition;
        if (startPosition &&
            startPosition.x === evt.clientX &&
            startPosition.y === evt.clientY) {
            return;
        }
        if (data.mouseMovedCount == null) {
            data.mouseMovedCount = 0;
        }
        data.mouseMovedCount += 1;
        const mouseMovedCount = data.mouseMovedCount;
        if (mouseMovedCount <= this.options.moveThreshold) {
            return;
        }
        const e = this.normalizeEvent(evt);
        const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
        const view = data.currentView;
        if (view) {
            view.onMouseMove(e, localPoint.x, localPoint.y);
        }
        else {
            this.graph.trigger('blank:mousemove', {
                e,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
        this.setEventData(e, data);
    }
    onMouseUp(e) {
        this.undelegateDocumentEvents();
        const normalized = this.normalizeEvent(e);
        const localPoint = this.graph.snapToGrid(normalized.clientX, normalized.clientY);
        const data = this.getEventData(e);
        const view = data.currentView;
        if (view) {
            view.onMouseUp(normalized, localPoint.x, localPoint.y);
        }
        else {
            this.graph.trigger('blank:mouseup', {
                e: normalized,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
        if (!e.isPropagationStopped()) {
            this.onClick(JQuery.Event(e, {
                type: 'click',
                data: e.data,
            }));
        }
        e.stopImmediatePropagation();
        this.delegateEvents();
    }
    onMouseOver(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        if (view) {
            view.onMouseOver(e);
        }
        else {
            // prevent border of paper from triggering this
            if (this.container === e.target) {
                return;
            }
            this.graph.trigger('blank:mouseover', { e });
        }
    }
    onMouseOut(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        if (view) {
            view.onMouseOut(e);
        }
        else {
            if (this.container === e.target) {
                return;
            }
            this.graph.trigger('blank:mouseout', { e });
        }
    }
    onMouseEnter(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        const relatedView = this.graph.renderer.findViewByElem(e.relatedTarget);
        if (view) {
            if (relatedView === view) {
                // mouse moved from tool to view
                return;
            }
            view.onMouseEnter(e);
        }
        else {
            if (relatedView) {
                return;
            }
            this.graph.trigger('graph:mouseenter', { e });
        }
    }
    onMouseLeave(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        const relatedView = this.graph.renderer.findViewByElem(e.relatedTarget);
        if (view) {
            if (relatedView === view) {
                // mouse moved from view to tool
                return;
            }
            view.onMouseLeave(e);
        }
        else {
            if (relatedView) {
                return;
            }
            this.graph.trigger('graph:mouseleave', { e });
        }
    }
    onMouseWheel(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.guard(e, view)) {
            return;
        }
        const originalEvent = e.originalEvent;
        const localPoint = this.graph.snapToGrid(originalEvent.clientX, originalEvent.clientY);
        const delta = Math.max(-1, Math.min(1, originalEvent.wheelDelta || -originalEvent.detail));
        if (view) {
            view.onMouseWheel(e, localPoint.x, localPoint.y, delta);
        }
        else {
            this.graph.trigger('blank:mousewheel', {
                e,
                delta,
                x: localPoint.x,
                y: localPoint.y,
            });
        }
    }
    onCustomEvent(evt) {
        const elem = evt.currentTarget;
        const event = elem.getAttribute('event') || elem.getAttribute('data-event');
        if (event) {
            const view = this.findView(elem);
            if (view) {
                const e = this.normalizeEvent(evt);
                if (this.guard(e, view)) {
                    return;
                }
                const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
                view.onCustomEvent(e, event, localPoint.x, localPoint.y);
            }
        }
    }
    handleMagnetEvent(evt, handler) {
        const magnetElem = evt.currentTarget;
        const magnetValue = magnetElem.getAttribute('magnet');
        if (magnetValue && magnetValue.toLowerCase() !== 'false') {
            const view = this.findView(magnetElem);
            if (view) {
                const e = this.normalizeEvent(evt);
                if (this.guard(e, view)) {
                    return;
                }
                const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
                FunctionExt.call(handler, this.graph, view, e, magnetElem, localPoint.x, localPoint.y);
            }
        }
    }
    onMagnetMouseDown(e) {
        this.handleMagnetEvent(e, (view, e, magnet, x, y) => {
            view.onMagnetMouseDown(e, magnet, x, y);
        });
    }
    onMagnetDblClick(e) {
        this.handleMagnetEvent(e, (view, e, magnet, x, y) => {
            view.onMagnetDblClick(e, magnet, x, y);
        });
    }
    onMagnetContextMenu(evt) {
        const e = this.normalizeEvent(evt);
        const view = this.findView(e.target);
        if (this.isPreventDefaultContextMenu(e, view)) {
            e.preventDefault();
        }
        this.handleMagnetEvent(e, (view, e, magnet, x, y) => {
            view.onMagnetContextMenu(e, magnet, x, y);
        });
    }
    onLabelMouseDown(evt) {
        const labelNode = evt.currentTarget;
        const view = this.findView(labelNode);
        if (view) {
            const e = this.normalizeEvent(evt);
            if (this.guard(e, view)) {
                return;
            }
            const localPoint = this.graph.snapToGrid(e.clientX, e.clientY);
            view.onLabelMouseDown(e, localPoint.x, localPoint.y);
        }
    }
    onImageDragStart() {
        // This is the only way to prevent image dragging in Firefox that works.
        // Setting -moz-user-select: none, draggable="false" attribute or
        // user-drag: none didn't help.
        return false;
    }
    dispose() {
        this.undelegateEvents();
        this.undelegateDocumentEvents();
        this.restore();
        this.restore = () => { };
    }
}
__decorate([
    View.dispose()
], GraphView.prototype, "dispose", null);
(function (GraphView) {
    const prefixCls = `${Config.prefixCls}-graph`;
    GraphView.markup = [
        {
            ns: Dom.ns.xhtml,
            tagName: 'div',
            selector: 'background',
            className: `${prefixCls}-background`,
        },
        {
            ns: Dom.ns.xhtml,
            tagName: 'div',
            selector: 'grid',
            className: `${prefixCls}-grid`,
        },
        {
            ns: Dom.ns.svg,
            tagName: 'svg',
            selector: 'svg',
            className: `${prefixCls}-svg`,
            attrs: {
                width: '100%',
                height: '100%',
                'xmlns:xlink': Dom.ns.xlink,
            },
            children: [
                {
                    tagName: 'defs',
                    selector: 'defs',
                },
                {
                    tagName: 'g',
                    selector: 'viewport',
                    className: `${prefixCls}-svg-viewport`,
                    children: [
                        {
                            tagName: 'g',
                            selector: 'primer',
                            className: `${prefixCls}-svg-primer`,
                        },
                        {
                            tagName: 'g',
                            selector: 'stage',
                            className: `${prefixCls}-svg-stage`,
                        },
                        {
                            tagName: 'g',
                            selector: 'decorator',
                            className: `${prefixCls}-svg-decorator`,
                        },
                        {
                            tagName: 'g',
                            selector: 'overlay',
                            className: `${prefixCls}-svg-overlay`,
                        },
                    ],
                },
            ],
        },
    ];
    function snapshoot(elem) {
        const cloned = elem.cloneNode();
        elem.childNodes.forEach((child) => cloned.appendChild(child));
        return () => {
            // remove all children
            Dom.empty(elem);
            // remove all attributes
            while (elem.attributes.length > 0) {
                elem.removeAttribute(elem.attributes[0].name);
            }
            // restore attributes
            for (let i = 0, l = cloned.attributes.length; i < l; i += 1) {
                const attr = cloned.attributes[i];
                elem.setAttribute(attr.name, attr.value);
            }
            // restore children
            cloned.childNodes.forEach((child) => elem.appendChild(child));
        };
    }
    GraphView.snapshoot = snapshoot;
})(GraphView || (GraphView = {}));
(function (GraphView) {
    const prefixCls = Config.prefixCls;
    GraphView.events = {
        dblclick: 'onDblClick',
        contextmenu: 'onContextMenu',
        touchstart: 'onMouseDown',
        mousedown: 'onMouseDown',
        mouseover: 'onMouseOver',
        mouseout: 'onMouseOut',
        mouseenter: 'onMouseEnter',
        mouseleave: 'onMouseLeave',
        mousewheel: 'onMouseWheel',
        DOMMouseScroll: 'onMouseWheel',
        [`mouseenter  .${prefixCls}-cell`]: 'onMouseEnter',
        [`mouseleave  .${prefixCls}-cell`]: 'onMouseLeave',
        [`mouseenter  .${prefixCls}-cell-tools`]: 'onMouseEnter',
        [`mouseleave  .${prefixCls}-cell-tools`]: 'onMouseLeave',
        [`mousedown   .${prefixCls}-cell [event]`]: 'onCustomEvent',
        [`touchstart  .${prefixCls}-cell [event]`]: 'onCustomEvent',
        [`mousedown   .${prefixCls}-cell [data-event]`]: 'onCustomEvent',
        [`touchstart  .${prefixCls}-cell [data-event]`]: 'onCustomEvent',
        [`dblclick    .${prefixCls}-cell [magnet]`]: 'onMagnetDblClick',
        [`contextmenu .${prefixCls}-cell [magnet]`]: 'onMagnetContextMenu',
        [`mousedown   .${prefixCls}-cell [magnet]`]: 'onMagnetMouseDown',
        [`touchstart  .${prefixCls}-cell [magnet]`]: 'onMagnetMouseDown',
        [`dblclick    .${prefixCls}-cell [data-magnet]`]: 'onMagnetDblClick',
        [`contextmenu .${prefixCls}-cell [data-magnet]`]: 'onMagnetContextMenu',
        [`mousedown   .${prefixCls}-cell [data-magnet]`]: 'onMagnetMouseDown',
        [`touchstart  .${prefixCls}-cell [data-magnet]`]: 'onMagnetMouseDown',
        [`dragstart   .${prefixCls}-cell image`]: 'onImageDragStart',
        [`mousedown   .${prefixCls}-edge .${prefixCls}-edge-label`]: 'onLabelMouseDown',
        [`touchstart  .${prefixCls}-edge .${prefixCls}-edge-label`]: 'onLabelMouseDown',
    };
    GraphView.documentEvents = {
        mousemove: 'onMouseMove',
        touchmove: 'onMouseMove',
        mouseup: 'onMouseUp',
        touchend: 'onMouseUp',
        touchcancel: 'onMouseUp',
    };
})(GraphView || (GraphView = {}));
//# sourceMappingURL=view.js.map