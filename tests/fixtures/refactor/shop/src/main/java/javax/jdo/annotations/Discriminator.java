package javax.jdo.annotations;

public @interface Discriminator {
    DiscriminatorStrategy strategy();

    String column() default "";
}
