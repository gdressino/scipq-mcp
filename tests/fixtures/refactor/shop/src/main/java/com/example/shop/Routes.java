package com.example.shop;

/** open() reaches done() two shortest ways, both through step(), and a longer one. */
public class Routes {
    void open(boolean fast) {
        if (fast) {
            quick();
        } else {
            slow();
        }
        far();
    }

    void quick() {
        step();
    }

    void slow() {
        step();
    }

    void step() {
        done();
    }

    void far() {
        farther();
    }

    void farther() {
        farthest();
    }

    void farthest() {
        done();
    }

    void done() {
    }
}
