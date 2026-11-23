package com.example.shop;

/** A screen: it extends Widget through BaseScreen. */
public class MainScreen extends BaseScreen {
    public String show() {
        return Steps.third();
    }

    private String render() {
        return Steps.fourth();
    }
}
