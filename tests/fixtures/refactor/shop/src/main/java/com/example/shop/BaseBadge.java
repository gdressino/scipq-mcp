package com.example.shop;

/** onAttach() is declared here; subclasses only change refresh(). */
public class BaseBadge {
    protected void onAttach() {
        refresh();
    }

    protected void refresh() {
    }
}
