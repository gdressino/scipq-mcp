package com.example.shop;

public class Panel {
    void refresh() {
    }

    void wire(Runnable r) {
        r.run();
    }

    void setup() {
        wire(new Redrawable() {
            @Override
            public void run() {
                refresh();
            }
        });
    }
}
