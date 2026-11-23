package javax.jdo.annotations;

public @interface Element {
    String column() default "";

    String mappedBy() default "";
}
