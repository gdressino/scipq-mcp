package com.example.shop;

/** Renders the view it holds: its own code runs on a Frame, not on the view. */
public class Frame {
    View view;

    String paint() {
        return view.render();
    }
}
